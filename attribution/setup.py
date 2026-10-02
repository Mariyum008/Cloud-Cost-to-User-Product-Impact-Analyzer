"""Turn a config file into database tables the SQL steps read: settings, apps, inputs and the resource map."""
import json
import tomllib
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SPLITS = {"sessions_by_host", "sessions", "owner"}
UNITS = {"active_users", "transactions", "none"}
RULE_TYPES = {"tag", "resource_group", "resource_name", "subscription"}
VM_TYPES = ("microsoft.compute/virtualmachines",)

# Input contract: column -> type. Required columns must exist; optional ones are filled with NULL when missing.
CONTRACT = {
    "cost": {"required": {"ChargePeriodStart": "TIMESTAMP", "ChargeCategory": "VARCHAR", "EffectiveCost": "DOUBLE", "ResourceId": "VARCHAR"},
             "optional": {"BilledCost": "DOUBLE", "ListCost": "DOUBLE", "ResourceName": "VARCHAR", "ResourceType": "VARCHAR",
                          "Tags": "VARCHAR", "x_ResourceGroupName": "VARCHAR", "x_ResourceType": "VARCHAR", "SubAccountName": "VARCHAR",
                          "ServiceCategory": "VARCHAR", "ServiceName": "VARCHAR", "CommitmentDiscountStatus": "VARCHAR",
                          "CommitmentDiscountName": "VARCHAR", "PricingQuantity": "DOUBLE", "PricingCategory": "VARCHAR",
                          "x_SkuMeterName": "VARCHAR", "x_SkuMeterCategory": "VARCHAR"}},
    "inventory": {"required": {"resource_id": "VARCHAR", "app_id": "VARCHAR"},
                  "optional": {"resource_name": "VARCHAR", "arm_type": "VARCHAR", "environment": "VARCHAR", "sku": "VARCHAR",
                               "deleted_date": "DATE"}},
    "sessions": {"required": {"session_date": "DATE", "app_id": "VARCHAR", "user_id": "VARCHAR", "duration_hours": "DOUBLE"},
                 "optional": {"resource_id": "VARCHAR", "cpu_share_pct": "DOUBLE"}},
    "users": {"required": {"user_id": "VARCHAR", "team": "VARCHAR"}, "optional": {"hire_date": "DATE", "leave_date": "DATE"}},
    "activity": {"required": {"date": "DATE", "app_id": "VARCHAR"},
                 "optional": {"active_users": "BIGINT", "sessions": "BIGINT", "session_hours": "DOUBLE", "transactions": "BIGINT",
                              "revenue_usd": "DOUBLE", "data_processed_gb": "DOUBLE"}},
    "metrics": {"required": {"date": "DATE", "resource_id": "VARCHAR"}, "optional": {"running_hours": "DOUBLE", "avg_cpu_pct": "DOUBLE"}},
    "ground_truth": {"required": {"anomaly_id": "VARCHAR", "app_id": "VARCHAR", "start_date": "DATE"},
                     "optional": {"anomaly_type": "VARCHAR", "category": "VARCHAR", "end_date": "DATE", "resource_ids": "VARCHAR",
                                  "expected_signal": "VARCHAR", "is_waste": "BOOLEAN", "description": "VARCHAR"}},
}


class ConfigError(Exception):
    pass


def load(path):
    """Read and validate the config; every problem is reported at once so it can be fixed in one pass."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    errs = []
    org, data, apps = cfg.get("organization", {}), cfg.get("data", {}), cfg.get("apps", [])
    if not org.get("name"):
        errs.append("[organization] name is required.")
    if not data.get("cost"):
        errs.append("[data] cost is required: the table or file holding your FOCUS cost export.")
    if not apps:
        errs.append("Add at least one [[apps]] entry.")
    ids = [a.get("id") for a in apps]
    if len(ids) != len(set(ids)):
        errs.append("App ids must be unique.")
    for a in apps:
        tag = f"App {a.get('id', '?')}"
        for key in ("id", "name", "team"):
            if not a.get(key):
                errs.append(f"{tag}: '{key}' is required.")
        if a.get("role", "app") not in ("app", "shared"):
            errs.append(f"{tag}: role must be 'app' or 'shared'.")
        if a.get("split", "owner") not in SPLITS:
            errs.append(f"{tag}: split must be one of {sorted(SPLITS)}.")
        if a.get("unit", "none") not in UNITS:
            errs.append(f"{tag}: unit must be one of {sorted(UNITS)}.")
    alloc = cfg.get("allocation", {})
    if alloc.get("session_weight", "hours_x_cpu") not in ("hours_x_cpu", "hours", "count"):
        errs.append("[allocation] session_weight must be hours_x_cpu, hours or count.")
    if alloc.get("shared_split", "proportional") not in ("proportional", "even"):
        errs.append("[allocation] shared_split must be proportional or even.")
    for i, r in enumerate(cfg.get("mapping", {}).get("rules", []), 1):
        t = r.get("type")
        if t not in RULE_TYPES:
            errs.append(f"Mapping rule {i}: type must be one of {sorted(RULE_TYPES)}.")
        elif t == "tag" and not r.get("key"):
            errs.append(f"Mapping rule {i}: tag rules need a 'key'.")
        elif t != "tag" and (not r.get("pattern") or r.get("app") not in ids):
            errs.append(f"Mapping rule {i}: needs a 'pattern' and an 'app' that exists in [[apps]].")
    if errs:
        raise ConfigError("Config problems:\n  - " + "\n  - ".join(errs))
    cfg["_path"] = str(path)
    return cfg


def _source_sql(con, src):
    # A source is a table/view in the database or a CSV/Parquet file path (globs allowed)
    if any(src.lower().endswith(x) for x in (".csv", ".parquet", ".csv.gz")) or "/" in src or "*" in src:
        p = src if Path(src).is_absolute() else str((ROOT / src).resolve())
        reader = "read_parquet" if ".parquet" in src.lower() else "read_csv_auto"
        return f"{reader}('{p}')"
    exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [src]).fetchone()[0]
    return f'"{src}"' if exists else None


def build_inputs(con, cfg):
    """Create in_<name> views that always have the contract's columns, empty when the input isn't provided."""
    provided = {}
    for name, spec in CONTRACT.items():
        src = cfg["data"].get(name, "")
        cols = {**spec["required"], **spec["optional"]}
        sql = _source_sql(con, src) if src else None
        if src and sql is None:
            raise ConfigError(f"[data] {name} = '{src}': no table or file by that name.")
        if sql:
            try:
                have = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {sql}").fetchall()}
            except Exception as e:
                raise ConfigError(f"[data] {name} = '{src}': could not read it ({str(e).splitlines()[0]})")
            missing = [c for c in spec["required"] if c.lower() not in have]
            if missing:
                raise ConfigError(f"[data] {name} is missing required column(s): {', '.join(missing)}")
            select = ", ".join(f'TRY_CAST("{have[c.lower()]}" AS {t}) AS "{c}"' if c.lower() in have else f'NULL::{t} AS "{c}"'
                               for c, t in cols.items())
            con.execute(f"CREATE OR REPLACE VIEW in_{name} AS SELECT {select} FROM {sql}")
        else:
            select = ", ".join(f'NULL::{t} AS "{c}"' for c, t in cols.items())
            con.execute(f"CREATE OR REPLACE VIEW in_{name} AS SELECT {select} WHERE false")
        provided[name] = bool(sql) and con.execute(f"SELECT count(*) > 0 FROM in_{name}").fetchone()[0]
    return provided


def register_config(con, cfg):
    """Write settings and app rules as tables so the SQL steps can read them."""
    org, head, alloc = cfg["organization"], cfg.get("headline", {}), cfg.get("allocation", {})
    settings = {"org_name": org["name"], "org_description": org.get("description", ""), "org_footer": org.get("footer", ""),
                "currency_symbol": org.get("currency_symbol", "$"), "headline_label": head.get("label", "transactions"),
                "headline_scale": str(head.get("scale", 1000)), "session_weight": alloc.get("session_weight", "hours_x_cpu"),
                "shared_split": alloc.get("shared_split", "proportional"), "config_path": cfg["_path"]}
    con.execute("CREATE OR REPLACE TABLE cfg_settings AS SELECT * FROM (SELECT unnest(?) AS key, unnest(?) AS value)",
                [list(settings), list(settings.values())])

    def group(a):
        # Dashboard heading for the app, unless the config sets one
        if a.get("group"):
            return a["group"]
        if a.get("role") == "shared":
            return "Shared platform"
        if a.get("customer_facing"):
            return "Customer-facing"
        return {"sessions_by_host": "Desktops and VMs", "sessions": "Internal apps"}.get(a.get("split", "owner"), "Team-owned")

    rows = []
    for a in cfg["apps"]:
        unit = a.get("unit", "none")
        default_label = {"active_users": "per active user", "transactions": "per 1,000 transactions"}.get(unit)
        rows.append({"app_id": a["id"], "app_name": a["name"], "owner_team": a["team"], "role": a.get("role", "app"),
                     "split": a.get("split", "owner"), "unit": unit, "unit_label": a.get("unit_label", default_label),
                     "unit_scale": 1000 if unit == "transactions" else 1, "headline": bool(a.get("headline")),
                     "serves_headline": bool(a.get("serves_headline")), "revenue": bool(a.get("revenue")),
                     "customer_facing": bool(a.get("customer_facing")), "app_group": group(a),
                     "aliases": json.dumps([x.lower() for x in a.get("aliases", [])])})
    con.register("_apps_df", pd.DataFrame(rows))
    con.execute("CREATE OR REPLACE TABLE cfg_apps AS SELECT * FROM _apps_df")
    con.unregister("_apps_df")
    # Effective split: apps set to split by sessions fall back to their owner when no sessions were provided
    con.execute("""
        CREATE OR REPLACE TABLE app_rules AS
        SELECT a.*, CASE WHEN a.role = 'shared' THEN 'shared'
                         WHEN a.split IN ('sessions', 'sessions_by_host') AND a.app_id IN (SELECT DISTINCT app_id FROM in_sessions)
                         THEN a.split ELSE 'owner' END AS effective_split
        FROM cfg_apps a""")


def _like(pattern):
    # Glob (* and ?) to SQL LIKE, escaping LIKE's own wildcards
    p = pattern.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return p.replace("*", "%").replace("?", "_").replace("'", "''")


def build_resource_map(con, cfg):
    """Assign each billed resource to an app: inventory first, then config rules in order. Unmatched stays unmapped."""
    mapping = cfg.get("mapping", {})
    parts = []
    if mapping.get("use_inventory", True):
        parts.append("""SELECT r.resource_id, i.app_id, 'inventory' AS source, 0 AS priority
                        FROM res r JOIN in_inventory i ON lower(i.resource_id) = lower(r.resource_id)
                        JOIN cfg_apps a ON a.app_id = i.app_id""")
    for k, rule in enumerate(mapping.get("rules", []), 1):
        t = rule["type"]
        if t == "tag":
            key = rule["key"].replace('"', '\\"').replace("'", "''")
            parts.append(f"""SELECT r.resource_id, n.app_id, 'tag:{key}' AS source, {k} AS priority FROM res r
                JOIN app_names n ON lower(json_extract_string(r.tags, '$."{key}"')) = n.name_key""")
        else:
            col = {"resource_group": "rg", "resource_name": "resource_name", "subscription": "sub"}[t]
            parts.append(f"""SELECT r.resource_id, '{rule['app']}' AS app_id, '{t}' AS source, {k} AS priority FROM res r
                WHERE lower(r.{col}) LIKE '{_like(rule['pattern'])}' ESCAPE '\\'""")
    none = "SELECT NULL::VARCHAR AS resource_id, NULL::VARCHAR AS app_id, NULL::VARCHAR AS source, NULL::INT AS priority WHERE false"
    candidates = " UNION ALL ".join(parts) if parts else none
    vm_types = ", ".join(f"'{t}'" for t in VM_TYPES)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE app_names AS
        SELECT app_id, lower(app_id) AS name_key FROM cfg_apps UNION SELECT app_id, lower(app_name) FROM cfg_apps
        UNION SELECT app_id, unnest(from_json(aliases, '["VARCHAR"]')) FROM cfg_apps;
        CREATE OR REPLACE TEMP TABLE res AS
        SELECT ResourceId AS resource_id, any_value(ResourceName) AS resource_name,
               lower(any_value(coalesce(x_ResourceType, ResourceType))) AS arm_type, any_value(Tags) AS tags,
               any_value(x_ResourceGroupName) AS rg, any_value(SubAccountName) AS sub
        FROM in_cost
        WHERE ResourceId IS NOT NULL AND coalesce(CommitmentDiscountStatus, '') <> 'Unused'
        GROUP BY 1;
        CREATE OR REPLACE TABLE resource_dim AS
        WITH candidates AS ({candidates}),
        best AS (SELECT resource_id, arg_min(app_id, priority) AS app_id, arg_min(source, priority) AS map_source
                 FROM candidates GROUP BY 1)
        SELECT r.resource_id, coalesce(i.resource_name, r.resource_name) AS resource_name,
               lower(coalesce(i.arm_type, r.arm_type)) AS arm_type, b.app_id, b.map_source,
               coalesce(i.environment, json_extract_string(r.tags, '$.env'), json_extract_string(r.tags, '$.environment')) AS environment,
               i.sku, i.deleted_date, lower(coalesce(i.arm_type, r.arm_type)) IN ({vm_types}) AS is_vm
        FROM res r
        LEFT JOIN best b USING (resource_id)
        LEFT JOIN in_inventory i ON lower(i.resource_id) = lower(r.resource_id);""")


def data_level(provided):
    # 1 = bill only, 2 = bill + daily activity, 3 = bill + per-user sessions
    return 3 if provided["sessions"] else 2 if provided["activity"] else 1
