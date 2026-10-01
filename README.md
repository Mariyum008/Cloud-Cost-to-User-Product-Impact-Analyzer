# Cloud Cost to User & Product Impact Analyzer

How does infrastructure spending translate into user activity and business outcomes?

Phase 1 is a synthetic but realistic company whose Azure bill, workforce activity, and customer demand are causally linked. Cost is derived from usage, and usage is derived from people and transactions. That means attribution and anomaly detection have a real signal to find, and there's a ground-truth answer key to score them against.

## Quick start

```bash
pip install -r requirements.txt
python -m generator                      # about 40s, writes data/synthetic/
python validation/validate_focus.py      # checks the cost data against FOCUS 1.0 + Microsoft open data
python -m attribution                    # Phase 2: splits every dollar to apps, teams and users (about 30s)
python -m dashboard                      # Phase 3: builds docs/index.html, the interactive dashboard
```

Close any notebook or DBeaver connection to `cloud_impact.duckdb` before running the generator or the attribution engine, since both write to it.

Options: `--seed 7` for a different company, `--users 500 --months 6` for a fast test run, `--csv` to also export every table as CSV for Power BI or Excel.

## The synthetic company

Contoso Retail India: 5,000 employees in 12 teams, 20 applications, 24 months (Oct 2024 - Sep 2026), Central India region with DR in South India.

| Kind | Apps | Runs on |
|---|---|---|
| Virtual desktops | Contact Center, Finance, Developer, Office pools | AVD session hosts (D4s/E8s/D8s v5), OS disks, FSLogix premium files |
| Internal web | Finance ERP, HR Portal, Sales CRM, Ticketing, Document Mgmt, BI | App Service plans, Azure SQL, storage, plus dev environments |
| Customer-facing | Customer Portal, E-commerce, Payments, Mobile backend | AKS, App Service, SQL, Cosmos DB, Redis, App Gateway, bandwidth |
| Data and ML | Data Warehouse, ETL, ML Platform | Synapse, Data Factory, Databricks, A100 GPU VMs, data lake |
| Shared | Dev/Test, Backup & DR, Shared Platform | Dev VMs, Recovery Services, VPN, Log Analytics, Key Vault |

Behaviour that is modelled: shift work in the contact center, autoscaling host pools, headcount growth and attrition, Indian holidays, payday and Diwali demand peaks, data growth, three reservations (one expires and isn't renewed), about 4% untagged spend, and a 5% EA discount.

## Tables

All tables are in `data/synthetic/cloud_impact.duckdb` and as Parquet files.

| Table | Grain | Purpose |
|---|---|---|
| `focus_cost` | resource x meter x day | Azure cost in FOCUS 1.0 format, same columns as a Microsoft FOCUS export (all 44 core columns + 17 `x_` columns) |
| `resources` | resource | Inventory (CMDB): app, environment, SKU, lifecycle, tags |
| `resource_daily_metrics` | resource x day | Running hours and average CPU for compute |
| `sessions` | session | Who used which app on which resource, for how long, CPU share, memory, business actions (4.4M rows) |
| `business_metrics` | app x day | Active users, session hours, transactions, revenue, data processed |
| `users`, `teams`, `apps` | dimension | Org structure and cost centers |
| `reservations` | reservation | Commitments and their terms |
| `anomalies_ground_truth` | event | Answer key: planted events, windows, affected resources, whether it's waste |

The key join path:

```
focus_cost.ResourceId -> resources.resource_id -> resources.app_id -> apps
                         resources.resource_id <- sessions.resource_id -> users -> teams
                         resources.app_id      -> business_metrics.app_id
```

Join cost to apps through `resources`, not through `Tags`. Some resources are untagged on purpose, just like real estates.

## Phase 2: attribution engine

`python -m attribution` runs the SQL in `sql/attribution/` (in file order), then `sql/metrics/`, then reconciles. Every dollar on the bill must land in exactly one place, and the run fails if it doesn't.

| Step | File | Output |
|---|---|---|
| 1 | `10_cost_resource_daily.sql` | `cost_resource_daily`: each resource-day classified as direct, shared, orphaned, or unused_reservation |
| 2 | `20_allocation_rules.sql` | `allocation_rules`: the written rules R1-R5 behind every split |
| 3 | `30_cost_app_daily.sql` | `cost_app_daily`: direct cost per app, plus its share of shared platform cost |
| 4 | `40_cost_user.sql` | `cost_user_monthly` (user x app x month) and `cost_idle` (cost nobody was using) |
| 5 | `50_cost_team_monthly.sql` | `cost_team_monthly`: every dollar per team, labelled user_activity, owned_app, idle_capacity, or waste |
| - | `metrics/10_metrics_views.sql` | `v_kpi_monthly`, `v_app_monthly`, `v_team_monthly`: the KPIs the dashboard will read |

The rules:

- **R1 AVD hosts:** each host-day is split across the sessions on it by `duration_hours x cpu_share_pct`. Host-days with nobody on them are idle.
- **R2 Everything else in a user-facing app:** split the same way across the app's sessions for the month. Always-on cost is split monthly so a lone weekend user doesn't absorb a full day.
- **R3 Shared platform:** split across apps by each app's share of that day's direct cost, then down to users via R2.
- **R4 Customer-facing apps, ETL, Dev/Test:** charged to the owner team and measured per transaction instead of per user.
- **R5 Orphaned disks/IPs and unused reservations:** left unallocated so waste stays visible.

Reconciliation checks: resource layer = bill, apps + unallocated = bill, users + idle = session-app cost, and teams = bill.

Attribution answers *who* consumed cost. Waste caused by low utilization (an idle GPU, Synapse not pausing, dev VMs without auto-shutdown) is still charged to the app that owns it. Finding that kind of waste is the job of anomaly detection and recommendations in later phases.

## Phase 3: dashboard

`python -m dashboard` runs the queries in `dashboard/queries.py` against the attributed database and writes `docs/index.html`: one self-contained file with the data, the page, and Chart.js inlined. Open it in any browser, no server needed. Rerun it whenever you regenerate data.

| Page | Answers |
|---|---|
| Overview | What did the month cost, is cost keeping pace with orders and headcount, and where did every dollar go? |
| Cost to serve | What does each app cost per user or per 1,000 transactions, and how unevenly is that spread across users? |
| Anomalies | Which days did cost jump without activity moving? A same-weekday baseline rule, scored against the answer key. |
| Savings | Rule-based findings for the latest month: orphaned resources, unused reservations, dev VMs without schedules, oversized databases, reservation candidates. |

To publish it on GitHub Pages: push the repo, then in the repo's **Settings > Pages**, set the source to the `main` branch and the `/docs` folder. The dashboard will be live at `https://<your-username>.github.io/<repo-name>/`.

The anomaly rule and savings checks are deliberately simple baselines. Phase 4 (anomaly detection) and Phase 6 (recommendations) replace them with stronger methods, measured against the same answer key.

## Ground truth

`anomalies_ground_truth` lists 11 events across five categories:

- **spike:** something broke, cost jumps with no business reason.
- **step_change:** a permanent shift, such as a resize that was never reverted.
- **rate_change:** same usage at a higher price, such as an expired reservation.
- **legit_growth:** Diwali demand. This is a negative control: cost rises with transactions and should not be flagged.
- **ongoing_waste:** a continuous inefficiency, such as orphaned disks or dev VMs without auto-shutdown.

Use the table to score detectors (precision, recall, time to detect) in Phase 4. If you want to test blind first, don't read it.

## Validation

`validation/validate_focus.py` checks FOCUS 1.0 core columns, allowed values, service/category pairs and regions against Microsoft's `Services.csv` and `Regions.csv`, non-negative costs, period alignment, `ListCost = PricingQuantity x ListUnitPrice`, reservation rules, and that amortization closes (monthly BilledCost equals EffectiveCost). It writes `reports/validation_report.md` and exits non-zero on failure.

To also compare against Microsoft's real sample export, download `src/open-data/dataset-examples/EA-Cost-FOCUS_1.0.csv` from [microsoft/finops-toolkit](https://github.com/microsoft/finops-toolkit). It's stored with Git LFS, so use the "Download raw file" button on GitHub or `git lfs pull`. Then run:

```bash
python validation/validate_focus.py --sample path/to/EA-Cost-FOCUS_1.0.csv
```

This adds column coverage, value-domain overlap, cost share by service category, unit-price ratios on shared meters, and daily grain. The sample comes from a small demo account, so treat cost-share differences as context, not failures.

## Layout

```
generator/      config.py (all knobs), demand.py (people, sessions, customers), infra.py (resources + metered usage),
                anomalies.py (planted events), focus.py (FOCUS rows + reservations), __main__.py (pipeline)
validation/     validate_focus.py
attribution/    __main__.py (runs the SQL steps and reconciliation)
dashboard/      __main__.py (export), queries.py, template.html, vendor/ (Chart.js, MIT)
docs/           index.html, the built dashboard (served by GitHub Pages)
sql/            sanity_checks.sql, 01_cost_per_user_naive_vs_weighted.sql,
                attribution/ (steps 10-50), metrics/ (KPI views)
data/reference/ Microsoft FinOps toolkit open data (MIT, see LICENSE-finops-toolkit.txt)
```

## Assumptions and limits

- Prices are approximate Central India pay-as-you-go list prices in USD, rounded. Edit `PRICES` in `config.py` to change them, or plug in a real price sheet.
- Session `cpu_share_pct` is the share of the serving resource's CPU used by that session. It's the main attribution weight.
- One session per user per app per day.
- Dev/test and platform apps have no `business_metrics` rows. They are shared costs for the attribution engine to allocate.
- All data is synthetic. No employer or client data is used.
