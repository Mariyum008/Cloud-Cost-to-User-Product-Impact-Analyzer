-- Metrics layer: KPI views the dashboard and notebooks read. Labels and flags come from the config (cfg_apps).

-- Per app per month: cost, activity and unit costs
CREATE OR REPLACE VIEW v_app_monthly AS
WITH cost AS (
    SELECT date_trunc('month', date)::DATE AS month, app_id,
           sum(direct_cost) AS direct_cost, sum(shared_cost) AS shared_cost, sum(total_cost) AS total_cost
    FROM cost_app_daily GROUP BY ALL
),
idle AS (
    SELECT date_trunc('month', date)::DATE AS month, app_id, sum(cost) AS idle_cost
    FROM cost_idle GROUP BY ALL
),
activity AS (
    SELECT date_trunc('month', date)::DATE AS month, app_id,
           sum(session_hours) AS session_hours, sum(transactions) AS transactions, sum(revenue_usd) AS revenue_usd,
           sum(data_processed_gb) AS data_processed_gb,
           max(active_users) AS peak_daily_users   -- fallback when there's no session data
    FROM in_activity GROUP BY ALL
),
users AS (
    SELECT month, app_id, count(DISTINCT user_id) AS active_users
    FROM cost_user_monthly GROUP BY ALL
),
joined AS (
    SELECT c.month, c.app_id, a.app_name, a.app_group, a.owner_team, a.effective_split, a.unit, a.unit_label, a.unit_scale,
           a.customer_facing, c.direct_cost, c.shared_cost, c.total_cost, coalesce(i.idle_cost, 0) AS idle_cost,
           coalesce(u.active_users, ac.peak_daily_users) AS active_users, ac.session_hours, ac.transactions, ac.revenue_usd
    FROM cost c
    JOIN app_rules a USING (app_id)
    LEFT JOIN idle i USING (month, app_id)
    LEFT JOIN activity ac USING (month, app_id)
    LEFT JOIN users u USING (month, app_id)
)
SELECT month, app_id, app_name, app_group, owner_team, effective_split, unit, unit_label, customer_facing,
       round(direct_cost, 2) AS direct_cost,
       round(shared_cost, 2) AS shared_cost,
       round(total_cost, 2) AS total_cost,
       round(idle_cost, 2) AS idle_cost,
       round(100 * idle_cost / total_cost, 1) AS idle_pct,
       active_users,
       round(session_hours, 1) AS session_hours,
       transactions,
       revenue_usd,
       round(total_cost / nullif(active_users, 0), 2) AS cost_per_active_user,
       round(1000 * total_cost / nullif(transactions, 0), 3) AS cost_per_1k_transactions,
       -- the unit cost the config chose for this app
       round(CASE unit WHEN 'active_users' THEN total_cost / nullif(active_users, 0)
                       WHEN 'transactions' THEN unit_scale * total_cost / nullif(transactions, 0) END, 3) AS unit_cost,
       round(100 * (total_cost / lag(total_cost) OVER w - 1), 1) AS cost_mom_pct,
       round(100 * (transactions / lag(transactions) OVER w - 1), 1) AS transactions_mom_pct
FROM joined
WINDOW w AS (PARTITION BY app_id ORDER BY month);

-- Per team per month: cost, headcount and cost per head
CREATE OR REPLACE VIEW v_team_monthly AS
WITH months AS (SELECT DISTINCT month FROM cost_team_monthly),
headcount AS (
    -- people on the books on the 15th of each month (needs a users input)
    SELECT m.month, u.team, count(*) AS headcount
    FROM months m JOIN in_users u
      ON coalesce(u.hire_date, DATE '1900-01-01') <= m.month + INTERVAL 14 DAY
     AND coalesce(u.leave_date, DATE '9999-12-31') > m.month + INTERVAL 14 DAY
    GROUP BY ALL
)
SELECT c.month, c.team,
       round(sum(c.cost), 2) AS total_cost,
       round(sum(c.cost) FILTER (WHERE c.cost_type = 'user_activity'), 2) AS user_activity_cost,
       round(sum(c.cost) FILTER (WHERE c.cost_type = 'owned_app'), 2) AS owned_app_cost,
       round(sum(c.cost) FILTER (WHERE c.cost_type = 'idle_capacity'), 2) AS idle_capacity_cost,
       h.headcount,
       round(sum(c.cost) FILTER (WHERE c.cost_type = 'user_activity') / nullif(h.headcount, 0), 2) AS user_cost_per_head
FROM cost_team_monthly c
LEFT JOIN headcount h USING (month, team)
GROUP BY c.month, c.team, h.headcount;

-- Company-level KPIs per month. The headline metric (e.g. orders) comes from apps flagged in the config.
CREATE OR REPLACE VIEW v_kpi_monthly AS
WITH bill AS (
    SELECT date_trunc('month', ChargePeriodStart)::DATE AS month,
           sum(EffectiveCost) AS total_cost,
           sum(ListCost) FILTER (WHERE ChargeCategory = 'Usage') AS list_cost
    FROM in_cost GROUP BY ALL
),
classes AS (
    SELECT date_trunc('month', date)::DATE AS month,
           sum(cost) FILTER (WHERE cost_class = 'orphaned') AS orphaned_cost,
           sum(cost) FILTER (WHERE cost_class = 'unused_reservation') AS unused_reservation_cost,
           sum(cost) FILTER (WHERE cost_class = 'unmapped') AS unmapped_cost,
           sum(cost) FILTER (WHERE cost_class = 'other_charges') AS other_charges,
           sum(cost) FILTER (WHERE cost_class = 'shared') AS shared_cost
    FROM cost_resource_daily GROUP BY ALL
),
idle AS (SELECT date_trunc('month', date)::DATE AS month, sum(cost) AS idle_cost FROM cost_idle GROUP BY ALL),
internal AS (
    SELECT month, count(DISTINCT user_id) AS active_employees, sum(total_cost) AS internal_user_cost
    FROM cost_user_monthly GROUP BY ALL
),
business AS (
    SELECT v.month,
           sum(v.transactions) FILTER (WHERE a.headline) AS headline_volume,
           sum(v.revenue_usd) FILTER (WHERE a.revenue) AS revenue_usd,
           sum(v.total_cost) FILTER (WHERE a.customer_facing) AS customer_app_cost,
           sum(v.total_cost) FILTER (WHERE a.serves_headline) AS headline_cost
    FROM v_app_monthly v JOIN cfg_apps a USING (app_id) GROUP BY ALL
)
SELECT b.month,
       round(b.total_cost, 2) AS total_cost,
       round(100 * (b.total_cost / lag(b.total_cost) OVER (ORDER BY b.month) - 1), 1) AS total_cost_mom_pct,
       round(b.list_cost - b.total_cost, 2) AS savings_vs_list,
       i.active_employees,
       round(i.internal_user_cost / nullif(i.active_employees, 0), 2) AS cost_per_active_employee,
       bz.headline_volume,
       round(bz.headline_cost / nullif(bz.headline_volume, 0), 6) AS cost_per_headline_unit,
       round(bz.revenue_usd, 2) AS revenue_usd,
       round(100 * bz.customer_app_cost / nullif(bz.revenue_usd, 0), 3) AS customer_infra_pct_of_revenue,
       round(coalesce(id.idle_cost, 0), 2) AS idle_cost,
       round(coalesce(c.orphaned_cost, 0), 2) AS orphaned_cost,
       round(coalesce(c.unused_reservation_cost, 0), 2) AS unused_reservation_cost,
       round(coalesce(c.unmapped_cost, 0), 2) AS unmapped_cost,
       round(coalesce(c.other_charges, 0), 2) AS other_charges,
       round(100 * (coalesce(id.idle_cost, 0) + coalesce(c.orphaned_cost, 0)
             + coalesce(c.unused_reservation_cost, 0)) / b.total_cost, 1) AS idle_unallocated_pct
FROM bill b
LEFT JOIN classes c USING (month)
LEFT JOIN idle id USING (month)
LEFT JOIN internal i USING (month)
LEFT JOIN business bz USING (month)
ORDER BY b.month;
