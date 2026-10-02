"""Run the attribution engine: python -m attribution [--config config/contoso.toml]

Reads the config, builds the input views and resource map, runs sql/attribution/*.sql then sql/metrics/*.sql,
and proves every dollar is accounted for.
"""
import argparse
import sys
import time
from pathlib import Path

import duckdb

from . import setup

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 1e-6  # relative difference allowed between totals (floating-point rounding only)
UNALLOCATED = "('orphaned', 'unused_reservation', 'unmapped', 'other_charges')"
LEVELS = {1: "bill only", 2: "bill + daily activity", 3: "bill + activity + user sessions"}


def run_sql_folder(con, folder):
    # Each file may hold several statements; run them in file-name order
    for path in sorted((ROOT / "sql" / folder).glob("*.sql")):
        t0 = time.time()
        con.execute(path.read_text())
        print(f"  {folder}/{path.name:<32} {time.time() - t0:5.1f}s")


def reconcile(con):
    """Each check compares two totals that must match if no dollar was lost or double counted."""
    q = lambda sql: con.execute(sql).fetchone()[0] or 0.0
    bill = q("SELECT sum(EffectiveCost) FROM in_cost")
    checks = [
        ("Resource layer = bill", q("SELECT sum(cost) FROM cost_resource_daily"), bill),
        ("Apps + unallocated = bill",
         q("SELECT sum(total_cost) FROM cost_app_daily")
         + q(f"SELECT sum(cost) FROM cost_resource_daily WHERE cost_class IN {UNALLOCATED}")
         + q("SELECT sum(cost) FROM cost_team_monthly WHERE cost_type = 'shared_unspread'"), bill),
        ("Users + idle = session-app cost",
         q("SELECT sum(total_cost) FROM cost_user_monthly") + q("SELECT sum(cost) FROM cost_idle"),
         q("SELECT sum(total_cost) FROM cost_app_daily JOIN app_rules USING (app_id) "
           "WHERE effective_split IN ('sessions', 'sessions_by_host')")),
        ("Teams = bill", q("SELECT sum(cost) FROM cost_team_monthly"), bill),
    ]
    failed = 0
    for name, got, expected in checks:
        ok = abs(got - expected) <= TOLERANCE * max(abs(expected), 1)
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<36} {got:>14,.2f} vs {expected:>14,.2f}")
    # A negative app pool would mean host cost exceeded app cost, i.e. a broken split
    neg = q("SELECT count(*) FROM app_pool WHERE pool_direct < -0.01")
    print(f"  [{'PASS' if neg == 0 else 'FAIL'}] {'No negative cost pools':<36} {int(neg)} rows")
    return failed + (neg > 0)


def describe_setup(con, cfg, provided, level):
    # What was provided, and how much of the bill could be tied to an app
    print(f"Config: {cfg['_path']} ({cfg['organization']['name']})")
    print(f"Data level {level} of 3: {LEVELS[level]}")
    print("Inputs: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in provided.items()))
    cov = con.execute("""
        SELECT coalesce(d.map_source, 'unmapped') AS mapped_by, round(100 * sum(c.EffectiveCost) / sum(sum(c.EffectiveCost)) OVER (), 1) AS pct
        FROM in_cost c LEFT JOIN resource_dim d ON d.resource_id = c.ResourceId
        WHERE c.ChargeCategory = 'Usage' AND coalesce(c.CommitmentDiscountStatus, '') <> 'Unused'
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    print("Usage cost mapped to apps: " + ", ".join(f"{p}% by {s}" for s, p in cov))
    fallback = con.execute("SELECT string_agg(app_id, ', ') FROM app_rules WHERE split <> effective_split AND role = 'app'").fetchone()[0]
    if fallback:
        print(f"No session data for {fallback}; charged to their owner teams instead.")


def summary(con, symbol):
    # Short readout so you can see the engine's output immediately
    print("\nWhere the money went:")
    print(con.execute("""
        SELECT cost_type, round(sum(cost)) AS cost, round(100 * sum(cost) / sum(sum(cost)) OVER (), 1) AS pct
        FROM cost_team_monthly GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    print("\nLast 3 months:")
    print(con.execute("""
        SELECT month, total_cost, total_cost_mom_pct AS mom_pct, cost_per_active_employee AS per_employee,
               round(1000 * cost_per_headline_unit, 2) AS per_1k_headline, idle_unallocated_pct
        FROM v_kpi_monthly ORDER BY month DESC LIMIT 3""").df().to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "contoso.toml"))
    ap.add_argument("--db", help="override [data] database from the config")
    a = ap.parse_args()
    try:
        cfg = setup.load(a.config)
    except setup.ConfigError as e:
        sys.exit(str(e))
    db = Path(a.db or cfg["data"].get("database", "data/analyzer.duckdb"))
    db = db if db.is_absolute() else ROOT / db
    db.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db))
    try:
        provided = setup.build_inputs(con, cfg)
    except setup.ConfigError as e:
        sys.exit(str(e))
    if not provided["cost"]:
        sys.exit("The cost input is empty. Check [data] cost in the config.")
    setup.register_config(con, cfg)
    setup.build_resource_map(con, cfg)
    level = setup.data_level(provided)
    con.execute("CREATE OR REPLACE TABLE run_info AS SELECT ? AS data_level, ? AS level_name, now() AS run_at, ? AS inputs",
                [level, LEVELS[level], ", ".join(k for k, v in provided.items() if v)])
    describe_setup(con, cfg, provided, level)

    print("\nBuilding attribution tables:")
    run_sql_folder(con, "attribution")
    print("Building metrics views:")
    run_sql_folder(con, "metrics")
    print("\nReconciliation:")
    failed = reconcile(con)
    summary(con, cfg["organization"].get("currency_symbol", "$"))
    con.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
