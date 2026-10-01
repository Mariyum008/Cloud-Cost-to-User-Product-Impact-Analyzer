"""Validate generated FOCUS cost data.

Checks run against the FOCUS 1.0 column spec and Microsoft open data (bundled in data/reference),
and optionally against Microsoft's sample export (EA-Cost-FOCUS_1.0.csv from the FinOps toolkit repo).

    python validation/validate_focus.py
    python validation/validate_focus.py --sample path/to/EA-Cost-FOCUS_1.0.csv
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "data" / "reference"
ALLOWED = {
    "ChargeCategory": ["Usage", "Purchase", "Tax", "Credit", "Adjustment"],
    "ChargeFrequency": ["One-Time", "Recurring", "Usage-Based"],
    "PricingCategory": ["Standard", "Dynamic", "Committed", "Other"],
    "CommitmentDiscountStatus": ["Used", "Unused"],
    "CommitmentDiscountType": ["Reservation", "Savings Plan"],
    "ServiceCategory": ["AI and Machine Learning", "Analytics", "Business Applications", "Compute", "Databases",
                        "Developer Tools", "Multicloud", "Identity", "Integration", "Internet of Things",
                        "Management and Governance", "Media", "Migration", "Mobile", "Networking", "Security",
                        "Storage", "Web", "Other"],
}


class Report:
    def __init__(self):
        self.lines, self.failed = [], 0

    def check(self, name, ok, detail="", warn_only=False):
        # Record one check; warnings do not fail the run
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        self.failed += status == "FAIL"
        self.lines.append(f"| {status} | {name} | {detail} |")
        print(f"[{status}] {name} {detail}")

    def section(self, title):
        self.lines += ["", f"## {title}", "", "| Status | Check | Detail |", "|---|---|---|"]

    def table(self, title, df):
        self.lines += ["", f"### {title}", "", df.to_markdown(index=False)]


def validate_generated(con, r):
    r.section("Spec and invariants")
    meta = json.load(open(REF / "FocusCost_1.0.json"))
    spec = [c["ColumnName"] for c in meta["Columns"]]
    have = set(con.execute("SELECT column_name FROM (DESCRIBE gen)").df()["column_name"])
    core_missing = [c for c in spec if not c.startswith("x_") and c not in have]
    r.check("All FOCUS 1.0 core columns present", not core_missing, ", ".join(core_missing))
    x_have = [c for c in spec if c.startswith("x_") and c in have]
    r.check("Microsoft x_ extension columns", True, f"{len(x_have)} of {sum(c.startswith('x_') for c in spec)} included")

    for col, values in ALLOWED.items():
        bad = con.execute(f"SELECT DISTINCT {col} FROM gen WHERE {col} IS NOT NULL AND {col} NOT IN ?",
                          [values]).df()[col].tolist()
        r.check(f"{col} values allowed by FOCUS", not bad, ", ".join(map(str, bad)))

    q = lambda sql: con.execute(sql).fetchone()[0]
    bad = q("""SELECT count(*) FROM (SELECT DISTINCT ServiceName, ServiceCategory FROM gen
               WHERE ResourceType <> 'Reservation') g
               ANTI JOIN svc s ON g.ServiceName = s.ServiceName AND g.ServiceCategory = s.ServiceCategory""")
    r.check("ServiceName/ServiceCategory pairs exist in Microsoft Services.csv", bad == 0, f"{bad} unknown pairs")
    bad = q("SELECT count(*) FROM (SELECT DISTINCT RegionId FROM gen) g ANTI JOIN reg ON g.RegionId = reg.RegionId")
    r.check("RegionId values exist in Microsoft Regions.csv", bad == 0, f"{bad} unknown regions")
    bad = q("SELECT count(*) FROM gen WHERE least(BilledCost, EffectiveCost, ListCost, ContractedCost) < 0")
    r.check("No negative costs", bad == 0, f"{bad} rows")
    bad = q("SELECT count(*) FROM gen WHERE ChargeCategory='Usage' AND EffectiveCost > ListCost + 1e-9")
    r.check("EffectiveCost <= ListCost on usage", bad == 0, f"{bad} rows")
    bad = q("""SELECT count(*) FROM gen WHERE ChargePeriodStart < BillingPeriodStart
               OR ChargePeriodEnd > BillingPeriodEnd""")
    r.check("Charge period inside billing period", bad == 0, f"{bad} rows")
    bad = q("SELECT count(*) FROM gen WHERE PricingCategory='Committed' AND BilledCost <> 0")
    r.check("Reservation-covered usage has BilledCost = 0", bad == 0, f"{bad} rows")
    bad = q("""SELECT count(*) FROM gen WHERE ChargeCategory='Usage' AND PricingQuantity > 0
               AND abs(ListCost - PricingQuantity * ListUnitPrice) > 1e-6 * greatest(ListCost, 1)""")
    r.check("ListCost = PricingQuantity x ListUnitPrice", bad == 0, f"{bad} rows")
    bad = q("""SELECT count(*) FROM gen WHERE ConsumedQuantity IS NOT NULL
               AND (ChargeCategory <> 'Usage' OR CommitmentDiscountStatus = 'Unused')""")
    r.check("ConsumedQuantity null for purchases and unused commitments", bad == 0, f"{bad} rows")
    gap = con.execute("""SELECT strftime(BillingPeriodStart, '%Y-%m') AS month,
                                round(sum(BilledCost) - sum(EffectiveCost), 4) AS diff
                         FROM gen GROUP BY 1 HAVING abs(diff) > 0.01 * sum(BilledCost) / 100""").df()
    r.check("Amortization closes: monthly BilledCost = EffectiveCost", gap.empty, f"{len(gap)} months off")


def compare_sample(con, r, sample):
    r.section("Comparison with Microsoft sample export")
    con.execute(f"CREATE VIEW smp AS SELECT * FROM read_csv('{sample}', auto_detect=true, sample_size=20000)")
    gen_cols = set(con.execute("SELECT column_name FROM (DESCRIBE gen)").df()["column_name"])
    smp_cols = set(con.execute("SELECT column_name FROM (DESCRIBE smp)").df()["column_name"])
    core_gap = sorted(c for c in smp_cols - gen_cols if not c.startswith("x_"))
    r.check("Sample core columns covered", not core_gap, ", ".join(core_gap))
    r.check("Sample x_ columns not generated", True, f"{len(smp_cols - gen_cols - set(core_gap))} (informational)")

    for col in ["ChargeCategory", "ChargeFrequency", "PricingCategory", "PricingUnit", "ServiceCategory"]:
        new = con.execute(f"""SELECT DISTINCT {col} FROM gen WHERE {col} IS NOT NULL
                              EXCEPT SELECT DISTINCT CAST({col} AS VARCHAR) FROM smp""").df()[col].tolist()
        r.check(f"{col} values also seen in sample", not new, ", ".join(map(str, new)), warn_only=True)

    share = con.execute("""
        WITH g AS (SELECT ServiceCategory, sum(EffectiveCost) c FROM gen GROUP BY 1),
             s AS (SELECT ServiceCategory, sum(TRY_CAST(EffectiveCost AS DOUBLE)) c FROM smp GROUP BY 1)
        SELECT coalesce(g.ServiceCategory, s.ServiceCategory) AS service_category,
               round(100 * g.c / sum(g.c) OVER (), 1) AS generated_pct,
               round(100 * s.c / sum(s.c) OVER (), 1) AS sample_pct
        FROM g FULL JOIN s USING (ServiceCategory) ORDER BY generated_pct DESC NULLS LAST""").df()
    r.table("Cost share by ServiceCategory", share)

    prices = con.execute("""
        WITH g AS (SELECT x_SkuMeterName m, median(ListUnitPrice) p FROM gen WHERE ChargeCategory='Usage' GROUP BY 1),
             s AS (SELECT x_SkuMeterName m, median(TRY_CAST(ListUnitPrice AS DOUBLE)) p FROM smp
                   WHERE ChargeCategory='Usage' GROUP BY 1)
        SELECT g.m AS meter, round(g.p, 4) AS generated, round(s.p, 4) AS sample, round(g.p / s.p, 2) AS ratio
        FROM g JOIN s USING (m) WHERE s.p > 0 ORDER BY meter""").df()
    if prices.empty:
        r.check("Unit prices comparable on shared meters", True, "no meter names in common", warn_only=True)
    else:
        off = prices[(prices.ratio < 0.5) | (prices.ratio > 2)]
        r.check("Unit prices within 0.5x-2x of sample on shared meters", off.empty,
                f"{len(prices)} shared meters, {len(off)} outside range", warn_only=True)
        r.table("Unit price comparison (shared meters)", prices)

    grain = con.execute("""
        SELECT 'generated' AS dataset, median(n) AS median_rows_per_resource_day FROM
          (SELECT count(*) n FROM gen WHERE ChargeCategory='Usage' GROUP BY ResourceId, ChargePeriodStart)
        UNION ALL SELECT 'sample', median(n) FROM
          (SELECT count(*) n FROM smp WHERE ChargeCategory='Usage' GROUP BY ResourceId, ChargePeriodStart)""").df()
    r.table("Daily grain", grain)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generated", default=str(ROOT / "data" / "synthetic" / "focus_cost.parquet"))
    ap.add_argument("--sample", help="Microsoft EA-Cost-FOCUS_1.0.csv (optional)")
    ap.add_argument("--report", default=str(ROOT / "reports" / "validation_report.md"))
    a = ap.parse_args()

    con = duckdb.connect()
    con.execute(f"CREATE VIEW gen AS SELECT * FROM read_parquet('{a.generated}')")
    con.execute(f"CREATE VIEW svc AS SELECT * FROM read_csv('{REF / 'Services.csv'}', header=true)")
    con.execute(f"CREATE VIEW reg AS SELECT DISTINCT RegionId FROM read_csv('{REF / 'Regions.csv'}', header=true)")
    r = Report()
    r.lines = ["# FOCUS validation report", "", f"Generated: `{a.generated}`", f"Sample: `{a.sample or 'not provided'}`"]
    validate_generated(con, r)
    if a.sample:
        compare_sample(con, r, a.sample)
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text("\n".join(r.lines) + "\n")
    print(f"\n{r.failed} failed checks. Report: {a.report}")
    sys.exit(1 if r.failed else 0)


if __name__ == "__main__":
    main()
