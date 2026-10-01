"""Resources each app runs on, and their daily metered usage driven by demand."""
import json

import numpy as np
import pandas as pd

from . import config as C
from .naming import avd_host_id, primary_resource_id, resource_group, resource_id, short, subscription_for

SQL_VCORES = {"A05": 16, "A06": 4, "A07": 8, "A08": 8, "A09": 4, "A10": 8, "A11": 8, "A12": 16, "A13": 8}


class Infra:
    def __init__(self, cal, rng):
        self.cal, self.rng = cal, rng
        self.resources, self.meters, self.telemetry = [], [], {}
        self.monthly = 1.0 / cal.days_in_month  # daily share of a per-month charge
        self.ones = np.ones(cal.n)

    def add(self, app, env, arm_type, name, role, parent=None, sku="", region=C.REGION_PRIMARY,
            created=0, deleted=None, rid=None, **attrs):
        # Register a resource; about 6% are left untagged, like real estates
        rid = rid or resource_id(app, env, arm_type, name, parent)
        sub_id, sub_name = subscription_for(app, env)
        owner = next(t for t in C.TEAMS if t[0] == app.owner)
        tags = {} if self.rng.random() < 0.06 else {
            "app": app.name, "env": env, "owner": app.owner, "costcenter": owner[2]}
        self.resources.append(dict(
            resource_id=rid, resource_name=name, arm_type=arm_type, app_id=app.app_id, environment=env,
            role=role, sku=sku, region_id=region, subscription_id=sub_id, subscription_name=sub_name,
            resource_group=resource_group(app, env), created_day=created, deleted_day=deleted,
            tags=json.dumps(tags), attrs=attrs))
        return len(self.resources) - 1

    def _life(self, r, arr):
        # Zero out days before creation and after deletion
        res, a = self.resources[r], np.asarray(arr, float).copy()
        a[: res["created_day"]] = 0
        if res["deleted_day"] is not None:
            a[res["deleted_day"]:] = 0
        return a

    def meter(self, r, key, qty):
        self.meters.append({"r": r, "key": key, "qty": np.maximum(self._life(r, qty), 0)})

    def metrics(self, r, hours, cpu):
        h = self._life(r, hours)
        self.telemetry[r] = [h, np.where(h > 0, np.clip(cpu, 0.5, 100), 0.0)]

    def growth(self, rate):
        return (1 + rate) ** self.cal.years

    # ---------- per-kind builders ----------
    def avd(self, app, drv, pool):
        hours_on = min(24.0, app.window_hours + 1.5)  # scaled-out hosts plus ramp time
        for i in range(pool["n_hosts"]):
            name = f"vm-{short(app)}-avd-{i:03d}"
            r = self.add(app, "prod", "microsoft.compute/virtualmachines", name, "avd_host",
                         sku=app.sku, rid=avd_host_id(app, i), host_index=i)
            hrs = np.where(i < app.min_hosts, 24.0, np.where(i < pool["peak"], hours_on, 0.0))
            self.meter(r, f"vm:{app.sku}", hrs)
            cpu = 3 + 100 * pool["host_cpu_hours"][i] / np.maximum(hrs, 1e-9)
            self.metrics(r, hrs, cpu)
            d = self.add(app, "prod", "microsoft.compute/disks", f"{name}-osdisk", "os_disk", sku="P10")
            self.meter(d, "disk:P10", self.monthly)
        # FSLogix profile share sized per adopted user
        st = self.add(app, "prod", "microsoft.storage/storageaccounts", f"st{short(app)}fslogix", "avd_profiles")
        self.meter(st, "files:premium", drv["adopters"] * 12 * self.growth(0.08) * self.monthly)

    def sql(self, app, env, vcores, gb):
        server = f"sql-{short(app)}-{env}"
        r = self.add(app, env, "microsoft.sql/servers/databases", f"sqldb-{short(app)}-{env}", "sql_db",
                     parent=server, sku=f"GP_Gen5_{vcores}", vcores=vcores)
        self.meter(r, "sql:vcore", 24.0 * vcores * self.ones)
        self.meter(r, "sql:storage", gb * self.monthly)
        return r

    def storage(self, app, name, gb, tier="storage:hot", writes=None, reads=None, env="prod", role="storage"):
        r = self.add(app, env, "microsoft.storage/storageaccounts", name, role)
        self.meter(r, tier, gb * self.monthly)
        if writes is not None:
            self.meter(r, "storage:writes", writes)
        if reads is not None:
            self.meter(r, "storage:reads", reads)
        return r

    def internal_web(self, app, drv):
        peak_users = drv["sessions"] * 0.5
        inst = np.clip(np.ceil(peak_users / 150), 2, 20)
        r = self.add(app, "prod", "microsoft.web/serverfarms", f"asp-{short(app)}-prod", "app_service",
                     sku="P2v3", rid=primary_resource_id(app))
        self.meter(r, "asp:P2v3", 24 * inst)
        self.metrics(r, 24 * inst, 10 + 55 * peak_users / (inst * 150))
        nr = self.add(app, "dev", "microsoft.web/serverfarms", f"asp-{short(app)}-dev", "app_service_nonprod", sku="P1v3")
        self.meter(nr, "asp:P1v3", 24 * self.ones)
        self.metrics(nr, 24 * self.ones, 4 + self.rng.normal(0, 1, self.cal.n))
        v = SQL_VCORES[app.app_id]
        db = self.sql(app, "prod", v, 180 * v * self.growth(0.25))
        act = drv["actions"]
        self.metrics(db, 24 * self.ones, 12 + 55 * act / max(act.max(), 1))
        self.sql(app, "dev", 2, 60 * self.growth(0.1))
        big = app.app_id == "A09"  # document management holds the big archive
        self.storage(app, f"st{short(app)}prod", (25000 if big else 300) * self.growth(0.4 if big else 0.2),
                     writes=act * 40, reads=act * 200)

    def customer(self, app, tx, ext_total):
        a = app.app_id
        if a == "A11":
            inst = np.clip(np.ceil(tx / 9000), 3, 30)
            r = self.add(app, "prod", "microsoft.web/serverfarms", f"asp-{short(app)}-prod", "app_service",
                         sku="P3v3", rid=primary_resource_id(app))
            self.meter(r, "asp:P3v3", 24 * inst)
            self.metrics(r, 24 * inst, 15 + 55 * tx / (inst * 9000))
            self._edge(app, tx, gb_per_tx=0.004)
            self._redis(app)
            db = self.sql(app, "prod", SQL_VCORES[a], 900 * self.growth(0.3))
            self.metrics(db, 24 * self.ones, 15 + 50 * tx / tx.max())
            self.storage(app, f"st{short(app)}prod", 2000 * self.growth(0.3), reads=tx * 30)
        elif a == "A12":
            self._aks(app, tx, "D8s_v5", per_node=2600, lo=8, hi=64)
            db = self.sql(app, "prod", SQL_VCORES[a], 2500 * self.growth(0.4))
            self.metrics(db, 24 * self.ones, 18 + 60 * tx / tx.max())
            self._cosmos(app, 6000 + tx * 0.25, 900 * self.growth(0.5))
            self._redis(app)
            self._edge(app, tx, gb_per_tx=0.025)
            self.storage(app, f"st{short(app)}media", 8000 * self.growth(0.35), reads=tx * 120, writes=tx * 4)
        elif a == "A13":
            self._aks(app, tx, "D4s_v5", per_node=9000, lo=3, hi=16)
            db = self.sql(app, "prod", SQL_VCORES[a], 1200 * self.growth(0.35))
            self.metrics(db, 24 * self.ones, 20 + 50 * tx / tx.max())
            kv = self.add(app, "prod", "microsoft.keyvault/vaults", f"kv-{short(app)}-prod", "key_vault")
            self.meter(kv, "kv:ops", tx * 6)
        elif a == "A14":
            inst = np.clip(np.ceil(tx / 45000), 3, 30)
            r = self.add(app, "prod", "microsoft.web/serverfarms", f"asp-{short(app)}-prod", "app_service",
                         sku="P2v3", rid=primary_resource_id(app))
            self.meter(r, "asp:P2v3", 24 * inst)
            self.metrics(r, 24 * inst, 15 + 55 * tx / (inst * 45000))
            self._cosmos(app, 3000 + tx * 0.03, 400 * self.growth(0.6))
            self._edge(app, tx, gb_per_tx=0.0004)
            self.storage(app, f"st{short(app)}prod", 1500 * self.growth(0.5), writes=tx * 2)

    def _aks(self, app, tx, sku, per_node, lo, hi):
        cl = self.add(app, "prod", "microsoft.containerservice/managedclusters", f"aks-{short(app)}-prod", "aks_cluster")
        self.meter(cl, "aks:uptime", 24 * self.ones)
        nodes = np.clip(np.ceil(tx / per_node), lo, hi)
        r = self.add(app, "prod", "microsoft.compute/virtualmachinescalesets", f"aks-nodepool1-{short(app)}-vmss",
                     "aks_nodes", sku=sku)
        self.meter(r, f"vm:{sku}", 24 * nodes)
        self.metrics(r, 24 * nodes, 20 + 55 * tx / (nodes * per_node))

    def _cosmos(self, app, rus, gb):
        r = self.add(app, "prod", "microsoft.documentdb/databaseaccounts", f"cosmos-{short(app)}-prod", "cosmos")
        self.meter(r, "cosmos:ru", 24 * rus)  # RU/s-hours, billed per 100
        self.meter(r, "cosmos:storage", gb * self.monthly)

    def _redis(self, app):
        r = self.add(app, "prod", "microsoft.cache/redis", f"redis-{short(app)}-prod", "redis", sku="P1")
        self.meter(r, "redis:P1", 24 * self.ones)

    def _edge(self, app, tx, gb_per_tx):
        r = self.add(app, "prod", "microsoft.network/applicationgateways", f"agw-{short(app)}-prod", "app_gateway")
        self.meter(r, "appgw:fixed", 24 * self.ones)
        self.meter(r, "appgw:cu", 24 * (2 + tx / 20000))
        self.meter(r, "bw:internet", tx * gb_per_tx)

    def data(self, app, drv, data_gb, ext_total):
        if app.app_id == "A15":
            on = np.where(self.cal.workday, 14.0, 0.0)  # paused nights and weekends
            r = self.add(app, "prod", "microsoft.synapse/workspaces/sqlpools", "dwprod", "synapse_pool",
                         parent=f"syn-{short(app)}-prod", sku="DW500c", rid=primary_resource_id(app))
            self.meter(r, "syn:dw500c", on)
            act = drv["actions"]
            self.metrics(r, on, 10 + 70 * act / max(act.max(), 1))
            self.storage(app, f"st{short(app)}lake", 30000 * self.growth(0.5), writes=data_gb * 500)
        else:
            adf = self.add(app, "prod", "microsoft.datafactory/factories", f"adf-{short(app)}-prod", "data_factory")
            self.meter(adf, "adf:runs", 2200 * self.growth(0.3) + ext_total / 400)
            self.meter(adf, "adf:diu", data_gb / 35)
            dbx = self.add(app, "prod", "microsoft.databricks/workspaces", f"dbw-{short(app)}-prod", "databricks_jobs")
            self.meter(dbx, "dbx:jobs", data_gb * 0.5)
            self.storage(app, f"st{short(app)}raw", 60000 * self.growth(0.45), tier="storage:cool", writes=data_gb * 2000)

    def ml(self, app, drv):
        ws = self.add(app, "prod", "microsoft.databricks/workspaces", f"dbw-{short(app)}-prod", "databricks_interactive",
                      rid=primary_resource_id(app))
        self.meter(ws, "dbx:allpurpose", drv["hours"] * 2.2)
        self.storage(app, f"st{short(app)}models", 5000 * self.growth(0.6))
        # GPU training VMs: on only during scheduled training runs
        run = np.zeros((4, self.cal.n))
        months = pd.Series(np.arange(self.cal.n)).groupby(self.cal.dates.to_period("M")).first().values
        for m0 in months:
            for _ in range(2):
                s = m0 + self.rng.integers(0, 24)
                gpus = self.rng.choice(4, 2, replace=False)
                run[gpus, s:s + self.rng.integers(2, 5)] = 1
        for g in range(4):
            name = f"vm-{short(app)}-gpu-{g:02d}"
            r = self.add(app, "prod", "microsoft.compute/virtualmachines", name, "gpu_vm", sku="NC24ads_A100_v4")
            hrs = 24 * run[g]
            self.meter(r, "vm:NC24ads_A100_v4", hrs)
            self.metrics(r, hrs, 85 + self.rng.normal(0, 5, self.cal.n))
            d = self.add(app, "prod", "microsoft.compute/disks", f"{name}-datadisk", "data_disk", sku="P30")
            self.meter(d, "disk:P30", self.monthly)

    def devtest(self, app):
        n = self.cal.n
        workday_hours = np.where(self.cal.workday, 11.0, 0.0)
        for i in range(70):
            created = 0 if i < 45 else int(self.rng.integers(30, max(31, n - 60)))
            deleted = int(self.rng.integers(created + 60, max(created + 61, n))) if self.rng.random() < 0.3 else None
            auto = self.rng.random() < 0.7  # 30% have no auto-shutdown
            sku = "D4s_v5" if self.rng.random() < 0.7 else "D8s_v5"
            name = f"vm-dev-{i:03d}"
            r = self.add(app, "dev", "microsoft.compute/virtualmachines", name, "devtest_vm", sku=sku,
                         created=created, deleted=deleted, auto_shutdown=auto)
            hrs = workday_hours if auto else 24.0 * self.ones
            self.meter(r, f"vm:{sku}", hrs)
            busy = np.where(self.cal.workday, 11 * 22.0, 0.0)  # CPU-hour % during the working day
            self.metrics(r, hrs, (busy + 2.0 * np.maximum(hrs - 11, 0)) / np.maximum(hrs, 1e-9))
            # 30% of deleted VMs leave their disk and IP behind
            orphan = deleted is not None and self.rng.random() < 0.3
            d = self.add(app, "dev", "microsoft.compute/disks", f"{name}-osdisk", "orphaned_disk" if orphan else "os_disk",
                         sku="P10", created=created, deleted=None if orphan else deleted, vm=name)
            self.meter(d, "disk:P10", self.monthly)
            if self.rng.random() < 0.4:
                p = self.add(app, "dev", "microsoft.network/publicipaddresses", f"pip-{name}",
                             "orphaned_ip" if orphan else "public_ip", created=created,
                             deleted=None if orphan else deleted, vm=name)
                self.meter(p, "pip:static", 24 * self.ones)

    def platform(self, app, headcount, total_sessions, ext_total):
        if app.app_id == "A19":
            rsv = self.add(app, "prod", "microsoft.recoveryservices/vaults", f"rsv-{short(app)}-backup", "backup_vault")
            self.meter(rsv, "backup:instance", 45 * self.monthly)
            self.meter(rsv, "backup:storage", 38000 * self.growth(0.2) * self.monthly)
            asr = self.add(app, "prod", "microsoft.recoveryservices/vaults", f"rsv-{short(app)}-dr", "dr_vault",
                           region=C.REGION_DR)
            self.meter(asr, "asr:instance", 30 * self.monthly)
            self.meter(asr, "bw:interregion", 650 * self.growth(0.25) * self.rng.lognormal(0, 0.08, self.cal.n))
        else:
            for i in range(2):
                g = self.add(app, "prod", "microsoft.network/virtualnetworkgateways", f"vpngw-hub-{i}", "vpn_gateway", sku="VpnGw2")
                self.meter(g, "vpn:VpnGw2", 24 * self.ones)
            lb = self.add(app, "prod", "microsoft.network/loadbalancers", "lb-hub-internal", "load_balancer")
            self.meter(lb, "lb:rules", 24 * self.ones)
            for i in range(8):
                p = self.add(app, "prod", "microsoft.network/publicipaddresses", f"pip-hub-{i}", "public_ip")
                self.meter(p, "pip:static", 24 * self.ones)
            law = self.add(app, "prod", "microsoft.operationalinsights/workspaces", "law-central-prod", "log_analytics")
            gb = (35 + total_sessions / 450 + ext_total / 6000) * self.rng.lognormal(0, 0.06, self.cal.n)
            self.meter(law, "law:ingest", gb)
            kv = self.add(app, "prod", "microsoft.keyvault/vaults", "kv-platform-prod", "key_vault")
            self.meter(kv, "kv:ops", headcount * 40 + ext_total * 0.5)
            self.storage(app, "stplatformlogarchive", 12000 * self.growth(0.5), tier="storage:cool")

    # ---------- outputs ----------
    def resources_frame(self):
        df = pd.DataFrame(self.resources)
        dates = self.cal.dates
        df["created_date"] = [dates[c] if c > 0 else pd.NaT for c in df["created_day"]]
        df["deleted_date"] = [dates[int(d)] if pd.notna(d) and d < self.cal.n else pd.NaT for d in df["deleted_day"]]
        df["auto_shutdown"] = [a.get("auto_shutdown") for a in df["attrs"]]
        return df.drop(columns=["created_day", "deleted_day", "attrs", "role"])

    def telemetry_frame(self):
        parts = []
        for r, (h, cpu) in self.telemetry.items():
            on = np.nonzero(h)[0]
            parts.append(pd.DataFrame({
                "date": self.cal.dates.values[on], "resource_id": self.resources[r]["resource_id"],
                "running_hours": h[on].round(2), "avg_cpu_pct": cpu[on].round(1)}))
        return pd.concat(parts, ignore_index=True)
