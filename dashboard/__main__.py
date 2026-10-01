"""Build the dashboard: python -m dashboard [--db ...] [--out docs/index.html]

Runs the dashboard queries against the attributed database, then writes one self-contained HTML file
(data, charts library and page in a single file) that opens offline, on GitHub Pages, or as a shared link.
"""
import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb

from .queries import QUERIES

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def clean(v):
    # JSON-safe value: dates as ISO strings, NaN/NA as null, floats trimmed
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()[:10]
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4)
    if hasattr(v, "item"):  # numpy scalars
        return clean(v.item())
    return v


def columns(con, sql):
    # Column-oriented result {col: [values]} is much smaller than a list of row objects
    rel = con.execute(sql)
    names = [d[0] for d in rel.description]
    rows = rel.fetchall()
    return {n: [clean(r[i]) for r in rows] for i, n in enumerate(names)}


def reshape_daily(d):
    # One shared date axis plus aligned arrays per app, so the page can plot any app instantly
    dates = sorted(set(d["date"]))
    pos = {x: i for i, x in enumerate(dates)}
    apps = {}
    for i, app in enumerate(d["app_id"]):
        a = apps.setdefault(app, {k: [None] * len(dates) for k in ("cost", "activity", "cpu")})
        j = pos[d["date"][i]]
        for k in ("cost", "activity", "cpu"):
            a[k][j] = d[k][i]
    return {"dates": dates, "apps": apps}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "synthetic" / "cloud_impact.duckdb"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "index.html"))
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"Database not found: {a.db}. Run `python -m generator` and `python -m attribution` first.")

    con = duckdb.connect(a.db, read_only=True)
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'cost_app_daily'").fetchone()[0]:
        sys.exit("Attribution tables missing. Run `python -m attribution` first.")
    data = {}
    for name, sql in QUERIES.items():
        data[name] = columns(con, sql)
        print(f"  {name:<20} {len(next(iter(data[name].values()), [])):>6} rows")
    con.close()
    data["daily"] = reshape_daily(data["daily"])
    data["meta"] = {"company": "Contoso Retail India", "built": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # Inline data and Chart.js into the template so the output is a single portable file
    html = (HERE / "template.html").read_text()
    html = html.replace("/*__CHARTJS__*/", (HERE / "vendor" / "chart.umd.js").read_text())
    html = html.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Dashboard written: {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
