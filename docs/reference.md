# Technical reference

Detailed documentation for the Cloud Cost to User & Product Impact Analyzer. For an overview, see the [README](../README.md).

## Contents

- [Tables](#tables)
- [Inputs for your own data](#inputs-for-your-own-data)
- [Mapping resources to apps](#mapping-resources-to-apps)
- [App settings](#app-settings)
- [Attribution engine](#attribution-engine)
- [Allocation rules](#allocation-rules)
- [Ground truth](#ground-truth)
- [Validation](#validation)
- [Assumptions and limits](#assumptions-and-limits)

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

Join cost to apps through `resources`, not through `Tags`. Some resources are untagged on purpose, as in real estates.

After `python -m attribution`, the database also holds the engine's outputs: `cost_resource_daily`, `cost_app_daily`, `cost_user_monthly`, `cost_idle`, `cost_team_monthly`, `allocation_rules`, and the views `v_kpi_monthly`, `v_app_monthly` and `v_team_monthly`.

## Inputs for your own data

Each `[data]` entry in a config file is a table name, a `.csv` or `.parquet` path (globs allowed), or `""` when you don't have it. Column templates with example rows are in `templates/`.

| Input | Required columns | Optional columns |
|---|---|---|
| `cost` | FOCUS `ChargePeriodStart`, `ChargeCategory`, `EffectiveCost`, `ResourceId` | the other FOCUS columns, especially `Tags`, `ListCost`, `CommitmentDiscount*`, `x_ResourceGroupName` |
| `inventory` | `resource_id`, `app_id` | `resource_name`, `arm_type`, `environment`, `sku`, `deleted_date` |
| `activity` | `date`, `app_id` | `active_users`, `sessions`, `session_hours`, `transactions`, `revenue_usd`, `data_processed_gb` |
| `sessions` | `session_date`, `app_id`, `user_id`, `duration_hours` | `resource_id`, `cpu_share_pct` |
| `users` | `user_id`, `team` | `hire_date`, `leave_date` |
| `metrics` | `date`, `resource_id` | `running_hours`, `avg_cpu_pct` |

Use pseudonymous user IDs (a hash, not an email). The engine never needs names.

## Mapping resources to apps

Each billed resource is assigned to an app by the inventory first, then by `[mapping] rules` in order: a tag whose value matches an app's id, name or alias, or a glob on resource group, resource name or subscription. Whatever nothing matches is reported as "Not mapped to an app" rather than guessed, and each run prints the share of cost each method covered.

## App settings

Each `[[apps]]` entry sets how its cost is split (`sessions_by_host`, `sessions`, or `owner`), its unit (`active_users`, `transactions`, or `none`), and flags for the company headline metric (`headline`, `serves_headline`, `revenue`, `customer_facing`). `role = "shared"` marks platform apps whose cost is spread across the others. Apps set to split by sessions fall back to their owner team when no session data is provided, so one config works at every data level.

## Attribution engine

`python -m attribution` builds input views from the config, maps resources to apps, runs the SQL in `sql/attribution/` in file order, then `sql/metrics/`, then reconciles.

| Step | File | Output |
|---|---|---|
| 0 | `attribution/setup.py` | `in_*` input views, `cfg_settings`, `cfg_apps`, `app_rules`, `resource_dim` (resource to app map) |
| 1 | `10_cost_resource_daily.sql` | `cost_resource_daily`: each resource-day classified as direct, shared, orphaned, unused_reservation, unmapped, or other_charges |
| 2 | `20_allocation_rules.sql` | `allocation_rules`: the written rules behind every split |
| 3 | `30_cost_app_daily.sql` | `cost_app_daily`: direct cost per app, plus its share of shared platform cost |
| 4 | `40_cost_user.sql` | `cost_user_monthly` (user x app x month) and `cost_idle` (cost nobody was using) |
| 5 | `50_cost_team_monthly.sql` | `cost_team_monthly`: every dollar per team, labelled by how it got there |
| - | `metrics/10_metrics_views.sql` | `v_kpi_monthly`, `v_app_monthly`, `v_team_monthly` |

Reconciliation checks, which fail the run if any total differs: resource layer = bill, apps + unallocated = bill, users + idle = session-app cost, and teams = bill.

## Allocation rules

- **R1 Split by host:** each host-day (VMs, desktops) is split across the sessions on it by the session weight (`hours_x_cpu` by default). Host-days with nobody on them are idle.
- **R2 Split by sessions:** the rest of a user-facing app's cost is split the same way across its sessions for the month, so a lone weekend user doesn't absorb a full day.
- **R3 Shared platform:** split across apps by each app's share of that day's direct cost (or evenly), then down to users via R2.
- **R4 Owner:** charged to the owner team and measured per transaction instead of per user.
- **R5 Orphaned, unused reservations, unmapped, taxes and credits:** left unallocated so they stay visible.

Attribution answers *who* consumed cost. Waste caused by low utilization (an idle GPU, a warehouse not pausing, dev VMs without auto-shutdown) is still charged to the app that owns it; finding it is the job of anomaly detection and the savings checks.

## Ground truth

`anomalies_ground_truth` lists 11 planted events across five categories:

- **spike:** something broke, and cost jumps with no business reason.
- **step_change:** a permanent shift, such as a resize that was never reverted.
- **rate_change:** same usage at a higher price, such as an expired reservation.
- **legit_growth:** Diwali demand. A negative control: cost rises with transactions and should not be flagged.
- **ongoing_waste:** a continuous inefficiency, such as orphaned disks or dev VMs without auto-shutdown.

Use it to score detectors on precision, recall and time to detect. To test blind first, don't read it.

## Validation

`validation/validate_focus.py` checks FOCUS 1.0 core columns, allowed values, service/category pairs and regions against Microsoft's `Services.csv` and `Regions.csv`, non-negative costs, period alignment, `ListCost = PricingQuantity x ListUnitPrice`, reservation rules, and that amortization closes (monthly BilledCost equals EffectiveCost). It writes `reports/validation_report.md` and exits non-zero on failure.

To also compare against Microsoft's real sample export, download `src/open-data/dataset-examples/EA-Cost-FOCUS_1.0.csv` from [microsoft/finops-toolkit](https://github.com/microsoft/finops-toolkit) (stored with Git LFS: use "Download raw file" or `git lfs pull`), then run:

```bash
python validation/validate_focus.py --sample path/to/EA-Cost-FOCUS_1.0.csv
```

## Assumptions and limits

- Prices are approximate Central India pay-as-you-go list prices in USD, rounded. Edit `PRICES` in `generator/config.py` to change them.
- Session `cpu_share_pct` is the share of the serving resource's CPU used by that session, and is the main attribution weight.
- One session per user per app per day.
- Dev/test and platform apps have no `business_metrics` rows; they are costs for the engine to allocate.
- The anomaly rule and savings checks are deliberately simple baselines, to be replaced by stronger methods measured against the same answer key.
- All data is synthetic. No employer or client data is used.
