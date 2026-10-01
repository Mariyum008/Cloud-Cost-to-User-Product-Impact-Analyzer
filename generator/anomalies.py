"""Planted cost events, plus a ground-truth table to score detection against."""
import numpy as np
import pandas as pd

from . import config as C


def _find(infra, role, app_id=None):
    return [i for i, r in enumerate(infra.resources) if r["role"] == role and (app_id is None or r["app_id"] == app_id)]


def _meters(infra, r, key_prefix=""):
    return [m for m in infra.meters if m["r"] == r and m["key"].startswith(key_prefix)]


def _ids(infra, rs):
    return ";".join(infra.resources[r]["resource_id"] for r in rs)


def apply(infra, cal):
    gt = []

    def record(aid, atype, category, app, start, end, rs, signal, waste, desc):
        gt.append(dict(anomaly_id=aid, anomaly_type=atype, category=category, app_id=app,
                       start_date=pd.Timestamp(start), end_date=pd.Timestamp(end) if end else pd.NaT,
                       resource_ids=_ids(infra, rs), expected_signal=signal, is_waste=waste, description=desc))

    # 1. Scaling plan disabled: every Finance AVD host stays on 24/7 for four weeks
    s, e = cal.idx("2025-07-07"), cal.idx("2025-08-04")
    hosts = _find(infra, "avd_host", "A02")
    for r in hosts:
        h, cpu = infra.telemetry[r]
        busy = np.where(h[s:e] > 0, (cpu[s:e] - 3) * h[s:e], 0)
        cpu[s:e] = 2 + busy / 24
        h[s:e] = 24
        _meters(infra, r, "vm:")[0]["qty"][s:e] = 24
    record("ANM-001", "AVD_SCALING_PLAN_DISABLED", "spike", "A02", "2025-07-07", "2025-08-03", hosts,
           "cost up, sessions flat, host CPU down", True,
           "Autoscale plan for the Finance host pool was disabled during maintenance and not re-enabled.")

    # 2. GPU VMs left running idle after a training run
    s, e = cal.idx("2025-12-05"), cal.idx("2025-12-30")
    gpus = _find(infra, "gpu_vm", "A17")[:2]
    for r in gpus:
        h, cpu = infra.telemetry[r]
        idle = h[s:e] == 0
        h[s:e][idle], cpu[s:e][idle] = 24, 1.5
        _meters(infra, r, "vm:")[0]["qty"][s:e] = 24
    record("ANM-002", "GPU_LEFT_RUNNING", "spike", "A17", "2025-12-05", "2025-12-29", gpus,
           "cost up, GPU CPU near zero, no training activity", True,
           "Two A100 VMs were not deallocated after a training run.")

    # 3. Debug logging left on in e-commerce floods the central Log Analytics workspace
    s, e = cal.idx("2026-02-09"), cal.idx("2026-02-24")
    law = _find(infra, "log_analytics")
    _meters(infra, law[0], "law:")[0]["qty"][s:e] *= 6
    record("ANM-003", "LOG_INGESTION_SPIKE", "spike", "A20", "2026-02-09", "2026-02-23", law,
           "shared platform cost up, no change in sessions or transactions", True,
           "Verbose logging enabled in the E-commerce Platform (A12) release; cost lands on the shared workspace.")

    # 4. Misconfigured DR replication re-syncs full disks across regions
    s, e = cal.idx("2026-05-11"), cal.idx("2026-05-21")
    dr = _find(infra, "dr_vault")
    _meters(infra, dr[0], "bw:")[0]["qty"][s:e] *= 12
    record("ANM-004", "CROSS_REGION_REPLICATION_SPIKE", "spike", "A19", "2026-05-11", "2026-05-20", dr,
           "bandwidth cost up, no business driver", True,
           "Site Recovery policy change forced full initial replication of all protected VMs.")

    # 5. E-commerce SQL scaled up for Diwali 2025 and never scaled back
    s = cal.idx("2025-11-20")
    db = _find(infra, "sql_db", "A12")
    for r in db:
        _meters(infra, r, "sql:vcore")[0]["qty"][s:] = 24 * 32
        infra.telemetry[r][1][s:] *= 0.55
        infra.resources[r]["sku"] = "GP_Gen5_32"
    record("ANM-005", "SQL_SCALE_UP_NOT_REVERTED", "step_change", "A12", "2025-11-20", None, db,
           "permanent step up in cost per transaction, DB CPU drops", True,
           "Database moved from 16 to 32 vCores for the festive peak; utilization stayed low afterwards.")

    # 6. Synapse pause automation broken: pool runs 24/7
    s, e = cal.idx("2026-07-01"), cal.idx("2026-07-27")
    pool = _find(infra, "synapse_pool", "A15")
    for r in pool:
        h, cpu = infra.telemetry[r]
        cpu[s:e] = (cpu[s:e] * h[s:e] + 3 * (24 - h[s:e])) / 24
        h[s:e] = 24
        _meters(infra, r, "syn:")[0]["qty"][s:e] = 24
    record("ANM-006", "SYNAPSE_PAUSE_FAILURE", "spike", "A15", "2026-07-01", "2026-07-26", pool,
           "cost up on nights and weekends, query volume flat", True,
           "Pause/resume runbook failed after a credential rotation.")

    # 7. Reservation expiry: same usage, higher rate
    name, key, apps, qty, start, term, _ = C.RESERVATIONS[1]
    end = pd.Timestamp(start) + pd.DateOffset(months=term)
    nodes = _find(infra, "aks_nodes", "A12")
    record("ANM-007", "RESERVATION_EXPIRED", "rate_change", "A12", end, None, nodes,
           "cost up with flat usage; EffectiveCost/ListCost ratio jumps", False,
           f"{name} expired and was not renewed; node hours moved to pay-as-you-go rates.")

    # 8. Legitimate festive demand (negative control: cost up with business)
    customer = [i for i, r in enumerate(infra.resources) if r["app_id"] in ("A11", "A12", "A13", "A14")]
    for k, dw in enumerate(C.DIWALI[:2], start=8):
        d = pd.Timestamp(dw)
        record(f"ANM-00{k}", "FESTIVE_DEMAND", "legit_growth", "A12", d - pd.Timedelta(days=29), d + pd.Timedelta(days=3),
               customer, "cost up in line with transactions and revenue", False,
               "Diwali sale season. Should not be flagged as waste.")

    # 9-10. Ongoing waste for the optimization engine
    orphans = _find(infra, "orphaned_disk") + _find(infra, "orphaned_ip")
    first = min((infra.resources[r]["deleted_day"] or cal.n) for r in _find(infra, "devtest_vm"))
    record("ANM-010", "ORPHANED_RESOURCES", "ongoing_waste", "A18", cal.dates[min(first, cal.n - 1)], None, orphans,
           "disks and IPs billed with no attached VM", True, "Dev VMs deleted without their disks and public IPs.")
    always_on = [r for r in _find(infra, "devtest_vm") if not infra.resources[r]["attrs"]["auto_shutdown"]]
    record("ANM-011", "DEVTEST_NO_AUTO_SHUTDOWN", "ongoing_waste", "A18", cal.dates[0], None, always_on,
           "24h running hours with near-zero CPU at night and weekends", True,
           "Dev/test VMs without an auto-shutdown schedule.")
    gt = pd.DataFrame(gt)
    return gt[gt["start_date"] <= cal.dates[-1]].reset_index(drop=True)  # drop events outside a short window
