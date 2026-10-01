"""Generate the synthetic company: python -m generator [--out data/synthetic] [--seed 42] [--csv]"""
import argparse
import shutil
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from . import config as C
from .anomalies import apply as apply_anomalies
from .demand import Calendar, app_sessions, external_demand, make_users
from .focus import FocusWriter
from .infra import Infra


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/synthetic")
    ap.add_argument("--seed", type=int, default=C.SEED)
    ap.add_argument("--users", type=int, default=C.N_USERS)
    ap.add_argument("--months", type=int, default=C.N_MONTHS)
    ap.add_argument("--csv", action="store_true", help="also export every table as CSV (for Power BI/Excel)")
    a = ap.parse_args()
    C.N_USERS, C.N_MONTHS = a.users, a.months
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    (out / "sessions").mkdir(parents=True)
    rng = np.random.default_rng(a.seed)
    t0 = time.time()

    # 1. Calendar and workforce
    cal = Calendar()
    users, team_idx, active, intensity = make_users(rng, cal)
    headcount = active.sum(axis=0).astype(float)
    users.to_parquet(out / "users.parquet", index=False)
    pd.DataFrame(C.TEAMS, columns=["team", "headcount_weight", "cost_center", "locations"]).assign(
        locations=lambda d: d["locations"].str.join(", ")).drop(columns="headcount_weight").to_parquet(
        out / "teams.parquet", index=False)
    pd.DataFrame([{"app_id": x.app_id, "app_name": x.name, "app_kind": x.kind, "owner_team": x.owner}
                  for x in C.APPS]).to_parquet(out / "apps.parquet", index=False)
    print(f"[1/5] {len(users):,} users over {cal.n} days")

    # 2. Internal sessions (one parquet per app keeps memory low)
    drivers, pools, next_id = {}, {}, 1
    for app in C.APPS:
        if app.users:
            df, drivers[app.app_id], pools[app.app_id] = app_sessions(
                app, users, team_idx, active, intensity, cal, rng, next_id)
            df.to_parquet(out / "sessions" / f"{app.app_id}.parquet", index=False)
            next_id += len(df)
    print(f"[2/5] {next_id - 1:,} sessions")

    # 3. Customer demand and data volume
    apps = {x.app_id: x for x in C.APPS}
    tx, festive = {}, None
    for aid in ("A11", "A12", "A14"):
        tx[aid], festive = external_demand(apps[aid], cal, rng)
    tx["A13"] = tx["A12"] * 1.07 * rng.lognormal(0, 0.02, cal.n)  # payments incl. retries/refunds
    ext_load = tx["A11"] + tx["A12"] + tx["A13"] + tx["A14"] / 10
    data_gb = (150 + (tx["A11"] + tx["A12"] + tx["A13"]) / 600 + tx["A14"] / 20000) * rng.lognormal(0, 0.05, cal.n)
    total_sessions = np.sum([d["sessions"] for d in drivers.values()], axis=0)

    # 4. Infrastructure and planted anomalies
    infra = Infra(cal, rng)
    for app in C.APPS:
        k = app.kind
        if k == "avd":
            infra.avd(app, drivers[app.app_id], pools[app.app_id])
        elif k == "internal_web":
            infra.internal_web(app, drivers[app.app_id])
        elif k == "customer":
            infra.customer(app, tx[app.app_id], ext_load)
        elif k == "data":
            infra.data(app, drivers.get(app.app_id), data_gb, ext_load)
        elif k == "ml":
            infra.ml(app, drivers[app.app_id])
        elif k == "devtest":
            infra.devtest(app)
        else:
            infra.platform(app, headcount, total_sessions, ext_load)
    gt = apply_anomalies(infra, cal)
    gt.to_parquet(out / "anomalies_ground_truth.parquet", index=False)
    infra.telemetry_frame().to_parquet(out / "resource_daily_metrics.parquet", index=False)
    print(f"[3/5] {len(infra.resources):,} resources, {len(infra.meters):,} meters, {len(gt)} planted events")

    # 5. FOCUS cost rows
    fw = FocusWriter(infra, cal, out / "focus_cost.parquet")
    fw.reservations().to_parquet(out / "reservations.parquet", index=False)
    rows = fw.write()
    infra.resources_frame().to_parquet(out / "resources.parquet", index=False)
    print(f"[4/5] {rows:,} FOCUS cost rows")

    # Business metrics: daily activity per app (devtest and platform are shared costs, so excluded)
    bm = []
    for app in C.APPS:
        d = drivers.get(app.app_id)
        row = pd.DataFrame({"date": cal.dates, "app_id": app.app_id})
        if d is not None:
            row["active_users"], row["sessions"] = d["users"].astype(int), d["sessions"].astype(int)
            row["session_hours"], row["transactions"] = d["hours"].round(1), d["actions"].astype(np.int64)
        if app.app_id in tx:
            row["transactions"] = tx[app.app_id].round().astype(np.int64)
        if app.app_id == "A12":
            row["revenue_usd"] = (tx["A12"] * app.revenue_per_tx * (1 + 0.12 * festive)).round(2)
        if app.app_id == "A16":
            row["transactions"] = (2200 * 1.3 ** cal.years + ext_load / 400).round().astype(np.int64)
        if app.app_id in ("A15", "A16"):
            row["data_processed_gb"] = (data_gb if app.app_id == "A16" else d["actions"] * 0.8).round(1)
        if app.kind not in ("devtest", "platform"):
            bm.append(row)
    pd.concat(bm, ignore_index=True).to_parquet(out / "business_metrics.parquet", index=False)

    build_duckdb(out, a.csv)
    print(f"[5/5] done in {time.time() - t0:.0f}s -> {out.resolve()}")


def build_duckdb(out, csv):
    # One portable database file with every table; optional CSV export
    db_path = out / "cloud_impact.duckdb"
    con = duckdb.connect(str(db_path))
    tables = ["teams", "users", "apps", "resources", "business_metrics", "resource_daily_metrics",
              "focus_cost", "reservations", "anomalies_ground_truth"]
    for t in tables:
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{out / (t + '.parquet')}')")
    con.execute(f"CREATE TABLE sessions AS SELECT * FROM read_parquet('{out / 'sessions' / '*.parquet'}')")
    if csv:
        (out / "csv").mkdir(exist_ok=True)
        for t in tables + ["sessions"]:
            con.execute(f"COPY {t} TO '{out / 'csv' / (t + '.csv')}' (HEADER)")
    summary = con.execute("""
        SELECT strftime(BillingPeriodStart, '%Y-%m') AS month, round(sum(EffectiveCost)) AS effective_cost
        FROM focus_cost GROUP BY 1 ORDER BY 1""").df()
    print(summary.to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
