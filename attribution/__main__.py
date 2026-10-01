"""Run the attribution engine: python -m attribution [--db data/synthetic/cloud_impact.duckdb]

Executes sql/attribution/*.sql then sql/metrics/*.sql in order, then proves every dollar is accounted for.
"""
import argparse
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 1e-6  # relative difference allowed between totals (floating-point rounding only)


def run_sql_folder(con, folder):
    # Each file may hold several statements; run them in file-name order
    for path in sorted((ROOT / "sql" / folder).glob("*.sql")):
        t0 = time.time()
        con.execute(path.read_text())
        print(f"  {folder}/{path.name:<32} {time.time() - t0:5.1f}s")


def reconcile(con):
    """Each check compares two totals that must match if no dollar was lost or double counted."""
    q = lambda sql: con.execute(sql).fetchone()[0]
    bill = q("SELECT sum(EffectiveCost) FROM focus_cost")
    session_apps = "(SELECT DISTINCT app_id FROM sessions)"
    checks = [
        ("Resource layer = bill", q("SELECT sum(cost) FROM cost_resource_daily"), bill),
        ("Apps + unallocated waste = bill",
         q("SELECT sum(total_cost) FROM cost_app_daily")
         + q("SELECT sum(cost) FROM cost_resource_daily WHERE cost_class IN ('orphaned', 'unused_reservation')"),
         bill),
        ("Users + idle = session-app cost",
         q("SELECT sum(total_cost) FROM cost_user_monthly") + q("SELECT sum(cost) FROM cost_idle"),
         q(f"SELECT sum(total_cost) FROM cost_app_daily WHERE app_id IN {session_apps}")),
        ("Teams = bill", q("SELECT sum(cost) FROM cost_team_monthly"), bill),
    ]
    failed = 0
    for name, got, expected in checks:
        ok = abs(got - expected) <= TOLERANCE * abs(expected)
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<36} {got:>14,.2f} vs {expected:>14,.2f}")
    # A negative app pool would mean host cost exceeded app cost, i.e. a broken split
    neg = q("SELECT count(*) FROM app_pool WHERE pool_direct < -0.01")
    print(f"  [{'PASS' if neg == 0 else 'FAIL'}] {'No negative cost pools':<36} {neg} rows")
    return failed + (neg > 0)


def summary(con):
    # Short readout so you can see the engine's output immediately
    print("\nWhere the money went (24 months):")
    print(con.execute("""
        SELECT cost_type, round(sum(cost)) AS cost, round(100 * sum(cost) / sum(sum(cost)) OVER (), 1) AS pct
        FROM cost_team_monthly GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    print("\nLast 3 months of KPIs:")
    print(con.execute("""
        SELECT month, total_cost, total_cost_mom_pct AS mom_pct, cost_per_active_employee AS per_employee,
               cost_per_order AS per_order, idle_unallocated_pct
        FROM v_kpi_monthly ORDER BY month DESC LIMIT 3""").df().to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "synthetic" / "cloud_impact.duckdb"))
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"Database not found: {a.db}. Run `python -m generator` first.")

    con = duckdb.connect(a.db)
    print("Building attribution tables:")
    run_sql_folder(con, "attribution")
    print("Building metrics views:")
    run_sql_folder(con, "metrics")
    print("\nReconciliation:")
    failed = reconcile(con)
    summary(con)
    con.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
