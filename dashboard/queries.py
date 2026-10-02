"""Every query the dashboard needs. They read the engine's tables and the in_* input views, so they work at any data level."""

# Latest month in the bill; findings are evaluated on it
LATEST = "(SELECT max(date_trunc('month', ChargePeriodStart))::DATE FROM in_cost)"

QUERIES = {
    "meta": """
        SELECT (SELECT value FROM cfg_settings WHERE key = 'org_name') AS name,
               (SELECT value FROM cfg_settings WHERE key = 'org_description') AS description,
               (SELECT value FROM cfg_settings WHERE key = 'org_footer') AS footer,
               (SELECT value FROM cfg_settings WHERE key = 'currency_symbol') AS currency_symbol,
               (SELECT value FROM cfg_settings WHERE key = 'headline_label') AS headline_label,
               (SELECT value FROM cfg_settings WHERE key = 'headline_scale')::INT AS headline_scale,
               data_level, level_name, inputs
        FROM run_info""",

    "kpi": "SELECT * FROM v_kpi_monthly ORDER BY month",

    "apps": """
        SELECT app_id, app_name, app_group, owner_team, role, effective_split, unit, unit_label, customer_facing
        FROM app_rules ORDER BY app_id""",

    "app_monthly": """
        SELECT month, app_id, total_cost, direct_cost, shared_cost, idle_cost, idle_pct, active_users,
               session_hours, transactions, revenue_usd, cost_per_active_user, cost_per_1k_transactions, unit_cost,
               cost_mom_pct, transactions_mom_pct
        FROM v_app_monthly ORDER BY app_id, month""",

    "team_monthly": "SELECT * FROM v_team_monthly ORDER BY month, total_cost DESC",

    # Monthly spend by service category (from the raw bill)
    "service_monthly": """
        SELECT date_trunc('month', ChargePeriodStart)::DATE AS month, coalesce(ServiceCategory, 'Other') AS category,
               round(sum(EffectiveCost), 2) AS cost
        FROM in_cost GROUP BY ALL HAVING abs(sum(EffectiveCost)) > 0.005 ORDER BY month, cost DESC""",

    "cost_type_monthly": """
        SELECT month, cost_type, round(sum(cost), 2) AS cost
        FROM cost_team_monthly GROUP BY ALL ORDER BY month""",

    # Spread of per-user cost for each session app and month (empty without session data)
    "user_spread": """
        WITH per_user AS (
            SELECT month, app_id, user_id, sum(total_cost) AS cost FROM cost_user_monthly GROUP BY ALL)
        SELECT month, app_id,
               round(quantile_cont(cost, 0.10), 2) AS p10, round(quantile_cont(cost, 0.25), 2) AS p25,
               round(median(cost), 2) AS p50, round(quantile_cont(cost, 0.75), 2) AS p75,
               round(quantile_cont(cost, 0.90), 2) AS p90, round(max(cost), 2) AS max
        FROM per_user GROUP BY ALL ORDER BY app_id, month""",

    # Daily cost of the resources each app owns, its activity, and hours-weighted CPU
    "daily": """
        WITH cost AS (
            SELECT date, app_id, sum(cost) AS cost FROM cost_resource_daily
            WHERE app_id IS NOT NULL GROUP BY ALL),
        activity AS (
            SELECT date, app_id, sum(coalesce(sessions, transactions)) AS activity FROM in_activity GROUP BY ALL),
        cpu AS (
            SELECT m.date, d.app_id,
                   sum(m.avg_cpu_pct * m.running_hours) / nullif(sum(m.running_hours), 0) AS cpu
            FROM in_metrics m JOIN resource_dim d USING (resource_id) GROUP BY ALL)
        SELECT c.date, c.app_id, round(c.cost, 2) AS cost, a.activity, round(p.cpu, 1) AS cpu
        FROM cost c
        LEFT JOIN activity a USING (date, app_id)
        LEFT JOIN cpu p USING (date, app_id)
        ORDER BY c.app_id, c.date""",

    # Answer-key events (synthetic data only) with the measured change vs the 28 days before
    "events": """
        WITH ev AS (
            SELECT *, coalesce(least(end_date, start_date + INTERVAL 27 DAY), start_date + INTERVAL 27 DAY)::DATE AS win_end
            FROM in_ground_truth),
        res AS (SELECT anomaly_id, unnest(string_split(coalesce(resource_ids, ''), ';')) AS resource_id FROM ev),
        measure AS (
            -- average cost per calendar day, so days a resource was off count as zero
            SELECT e.anomaly_id,
                   sum(c.cost) FILTER (WHERE c.date BETWEEN e.start_date AND e.win_end)
                       / (any_value(e.win_end) - any_value(e.start_date)::DATE + 1) AS cost_in,
                   sum(c.cost) FILTER (WHERE c.date BETWEEN e.start_date - INTERVAL 28 DAY
                                                       AND e.start_date - INTERVAL 1 DAY) / 28 AS cost_before
            FROM ev e JOIN res r USING (anomaly_id)
            JOIN (SELECT date, resource_id, sum(cost) AS cost FROM cost_resource_daily GROUP BY ALL) c
              ON c.resource_id = r.resource_id
            GROUP BY e.anomaly_id),
        act AS (
            SELECT e.anomaly_id,
                   avg(coalesce(b.sessions, b.transactions)) FILTER (WHERE b.date BETWEEN e.start_date AND e.win_end) AS act_in,
                   avg(coalesce(b.sessions, b.transactions)) FILTER (WHERE b.date BETWEEN e.start_date - INTERVAL 28 DAY
                                                                        AND e.start_date - INTERVAL 1 DAY) AS act_before
            FROM ev e JOIN in_activity b USING (app_id) GROUP BY e.anomaly_id),
        cpu AS (
            SELECT e.anomaly_id,
                   avg(m.avg_cpu_pct) FILTER (WHERE m.date BETWEEN e.start_date AND e.win_end) AS cpu_in,
                   avg(m.avg_cpu_pct) FILTER (WHERE m.date BETWEEN e.start_date - INTERVAL 28 DAY
                                                              AND e.start_date - INTERVAL 1 DAY) AS cpu_before
            FROM ev e JOIN res r USING (anomaly_id) JOIN in_metrics m USING (resource_id)
            GROUP BY e.anomaly_id)
        SELECT e.anomaly_id, e.anomaly_type, e.category, e.app_id, e.start_date, e.end_date, e.win_end, e.is_waste,
               e.expected_signal, e.description,
               -- no comparison when the 28-day baseline falls before the data starts
               CASE WHEN full_base THEN round(100 * (m.cost_in / nullif(m.cost_before, 0) - 1), 1) END AS cost_change_pct,
               CASE WHEN full_base THEN round(100 * (a.act_in / nullif(a.act_before, 0) - 1), 1) END AS activity_change_pct,
               CASE WHEN full_base THEN round(100 * (p.cpu_in / nullif(p.cpu_before, 0) - 1), 1) END AS cpu_change_pct
        FROM (SELECT *, start_date - INTERVAL 28 DAY >= (SELECT min(date) FROM cost_resource_daily) AS full_base FROM ev) e
        LEFT JOIN measure m USING (anomaly_id)
        LEFT JOIN act a USING (anomaly_id)
        LEFT JOIN cpu p USING (anomaly_id)
        ORDER BY e.start_date""",

    # Invoice view: cost per subscription and resource group, amortized (effective) and as billed
    "billing_monthly": """
        SELECT date_trunc('month', ChargePeriodStart)::DATE AS month,
               coalesce(SubAccountName, 'Unknown subscription') AS subscription,
               coalesce(lower(x_ResourceGroupName), '(no resource group)') AS resource_group,
               round(sum(EffectiveCost), 2) AS effective, round(sum(coalesce(BilledCost, EffectiveCost)), 2) AS billed
        FROM in_cost GROUP BY ALL
        HAVING abs(sum(EffectiveCost)) + abs(sum(coalesce(BilledCost, EffectiveCost))) > 0.005
        ORDER BY month, subscription, resource_group""",

    # What each resource group spends on, and which app owns most of it
    "rg_services": """
        SELECT date_trunc('month', ChargePeriodStart)::DATE AS month,
               coalesce(lower(x_ResourceGroupName), '(no resource group)') AS resource_group,
               coalesce(ServiceName, ServiceCategory, 'Other') AS service, round(sum(EffectiveCost), 2) AS cost
        FROM in_cost GROUP BY ALL HAVING sum(EffectiveCost) > 0.005 ORDER BY month, resource_group, cost DESC""",
    "rg_apps": """
        SELECT lower(c.x_ResourceGroupName) AS resource_group, arg_max(coalesce(a.app_name, 'Unmapped'), c.cost) AS app
        FROM (SELECT x_ResourceGroupName, ResourceId, sum(EffectiveCost) AS cost FROM in_cost
              WHERE x_ResourceGroupName IS NOT NULL GROUP BY ALL) c
        LEFT JOIN resource_dim d ON d.resource_id = c.ResourceId LEFT JOIN cfg_apps a ON a.app_id = d.app_id
        GROUP BY 1""",

    "waste_monthly": """
        SELECT month, idle_cost, orphaned_cost, unused_reservation_cost, unmapped_cost FROM v_kpi_monthly ORDER BY month""",

    # Reservation hours used vs unused per month
    "reservation_monthly": """
        SELECT date_trunc('month', ChargePeriodStart)::DATE AS month, CommitmentDiscountName AS reservation,
               round(sum(PricingQuantity) FILTER (WHERE CommitmentDiscountStatus = 'Used')) AS used,
               round(sum(PricingQuantity) FILTER (WHERE CommitmentDiscountStatus = 'Unused')) AS unused,
               round(sum(EffectiveCost) FILTER (WHERE CommitmentDiscountStatus = 'Unused'), 2) AS unused_cost
        FROM in_cost WHERE CommitmentDiscountStatus IS NOT NULL AND CommitmentDiscountName IS NOT NULL
        GROUP BY ALL ORDER BY reservation, month""",

    # Rule-based findings on the latest month. Rules that need data an organization didn't provide simply find nothing.
    "findings": f"""
        WITH latest AS (SELECT {LATEST} AS m),
        month_cost AS (
            SELECT c.resource_id, sum(c.cost) AS cost
            FROM cost_resource_daily c, latest WHERE date_trunc('month', c.date) = latest.m GROUP BY ALL),
        orphans AS (
            SELECT 'Delete orphaned disks and public IPs' AS action, coalesce(a.app_name, 'Unmapped') AS scope,
                   count(DISTINCT c.resource_id) AS resources, sum(c.cost) AS monthly_savings,
                   'Billed with no VM attached; their VMs were deleted.' AS evidence
            FROM cost_resource_daily c LEFT JOIN cfg_apps a USING (app_id), latest
            WHERE c.cost_class = 'orphaned' AND date_trunc('month', c.date) = latest.m GROUP BY ALL),
        unused_ri AS (
            SELECT 'Resize or exchange under-used reservation' AS action, f.CommitmentDiscountName AS scope,
                   1 AS resources, sum(f.EffectiveCost) AS monthly_savings,
                   printf('%.0f reserved hours went unused last month.', sum(f.PricingQuantity)) AS evidence
            FROM in_cost f, latest
            WHERE f.CommitmentDiscountStatus = 'Unused' AND date_trunc('month', f.ChargePeriodStart) = latest.m
            GROUP BY ALL),
        devtest AS (
            -- Non-production VMs still running through the weekend have no shutdown schedule
            SELECT d.resource_id, d.app_id
            FROM in_metrics m JOIN resource_dim d USING (resource_id), latest
            WHERE lower(d.environment) IN ('dev', 'test', 'nonprod', 'development') AND d.is_vm
              AND date_trunc('month', m.date) = latest.m AND dayofweek(m.date) IN (0, 6)
            GROUP BY ALL HAVING avg(m.running_hours) >= 20),
        schedule AS (
            SELECT 'Add auto-shutdown to non-production VMs' AS action, coalesce(a.app_name, 'Unmapped') AS scope,
                   count(*) AS resources, sum(mc.cost) * (1 - 55.0 / 168) AS monthly_savings,
                   'Running 24/7 including weekends; a weekday 11-hour schedule covers working time.' AS evidence
            FROM devtest d JOIN month_cost mc USING (resource_id) LEFT JOIN cfg_apps a USING (app_id) GROUP BY ALL),
        sql_cpu AS (
            SELECT d.resource_id, d.app_id, d.sku, avg(m.avg_cpu_pct) AS cpu
            FROM in_metrics m JOIN resource_dim d USING (resource_id), latest
            WHERE d.arm_type = 'microsoft.sql/servers/databases' AND coalesce(lower(d.environment), 'prod') = 'prod'
              AND date_trunc('month', m.date) = latest.m
            GROUP BY ALL HAVING avg(m.avg_cpu_pct) < 30
               AND TRY_CAST(regexp_extract(d.sku, '_(\\d+)$', 1) AS INT) >= 16),
        vcore AS (
            SELECT f.ResourceId AS resource_id, sum(f.EffectiveCost) AS cost
            FROM in_cost f, latest
            WHERE f.x_SkuMeterName = 'vCore' AND date_trunc('month', f.ChargePeriodStart) = latest.m GROUP BY ALL),
        rightsize AS (
            SELECT 'Halve vCores on under-used database' AS action, coalesce(a.app_name, 'Unmapped') || ' (' || s.sku || ')' AS scope,
                   1 AS resources, v.cost * 0.5 AS monthly_savings,
                   printf('Average CPU %.0f%% last month.', s.cpu) AS evidence
            FROM sql_cpu s JOIN vcore v USING (resource_id) LEFT JOIN cfg_apps a USING (app_id)),
        steady AS (
            -- Production compute billed fully on-demand that ran every day: the always-on floor can be reserved
            SELECT d.resource_id, d.app_id, x.sku, min(x.hours) AS floor_hours, sum(x.hours) AS hours, sum(x.cost) AS cost
            FROM (SELECT f.ResourceId AS resource_id, f.ChargePeriodStart::DATE AS day, any_value(f.x_SkuMeterName) AS sku,
                         sum(f.PricingQuantity) AS hours, sum(f.EffectiveCost) AS cost,
                         bool_and(f.PricingCategory = 'Standard') AS on_demand
                  FROM in_cost f, latest
                  WHERE f.x_SkuMeterCategory = 'Virtual Machines' AND f.ChargeCategory = 'Usage'
                    AND date_trunc('month', f.ChargePeriodStart) = latest.m
                  GROUP BY ALL) x
            JOIN resource_dim d USING (resource_id)
            WHERE coalesce(lower(d.environment), 'prod') = 'prod'
            GROUP BY ALL
            HAVING bool_and(x.on_demand) AND count(*) >= 28),
        reserve AS (
            SELECT 'Reserve always-on compute (1-year)' AS action, coalesce(a.app_name, 'Unmapped') || ' (' || s.sku || ')' AS scope,
                   s.n AS resources, s.floor_cost * 0.37 AS monthly_savings,
                   printf('%.0f%% of hours are a steady floor paid at on-demand rates.', 100 * s.floor_cost / s.cost) AS evidence
            FROM (SELECT app_id, sku, count(*) AS n, sum(cost) AS cost,
                         sum(cost * least(1, floor_hours * 30 / hours)) AS floor_cost
                  FROM steady GROUP BY ALL) s
            LEFT JOIN cfg_apps a USING (app_id))
        SELECT action, scope, resources, round(monthly_savings, 2) AS monthly_savings, evidence
        FROM (SELECT * FROM orphans UNION ALL SELECT * FROM unused_ri UNION ALL SELECT * FROM schedule
              UNION ALL SELECT * FROM rightsize UNION ALL SELECT * FROM reserve)
        WHERE monthly_savings > 1
        ORDER BY monthly_savings DESC""",
}
