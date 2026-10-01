-- Metrics layer: KPI views the dashboard and notebooks read directly.

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
           sum(session_hours) AS session_hours, sum(transactions) AS transactions,
           sum(revenue_usd) AS revenue_usd, sum(data_processed_gb) AS data_processed_gb
    FROM business_metrics GROUP BY ALL
),
users AS (
    SELECT month, app_id, count(DISTINCT user_id) AS active_users
    FROM cost_user_monthly GROUP BY ALL
)
SELECT c.month, c.app_id, a.app_name, a.app_kind, a.owner_team,
       round(c.direct_cost, 2) AS direct_cost,
       round(c.shared_cost, 2) AS shared_cost,
       round(c.total_cost, 2) AS total_cost,
       round(coalesce(i.idle_cost, 0), 2) AS idle_cost,
       round(100 * coalesce(i.idle_cost, 0) / c.total_cost, 1) AS idle_pct,
       u.active_users,
       round(ac.session_hours, 1) AS session_hours,
       ac.transactions,
       ac.revenue_usd,
       round(c.total_cost / nullif(u.active_users, 0), 2) AS cost_per_active_user,
       round(c.total_cost / nullif(ac.session_hours, 0), 3) AS cost_per_session_hour,
       round(1000 * c.total_cost / nullif(ac.transactions, 0), 3) AS cost_per_1k_transactions,
       round(100 * (c.total_cost / lag(c.total_cost) OVER w - 1), 1) AS cost_mom_pct,
       round(100 * (ac.transactions / lag(ac.transactions) OVER w - 1), 1) AS transactions_mom_pct
FROM cost c
JOIN apps a USING (app_id)
LEFT JOIN idle i USING (month, app_id)
LEFT JOIN activity ac USING (month, app_id)
LEFT JOIN users u USING (month, app_id)
WINDOW w AS (PARTITION BY c.app_id ORDER BY c.month);

-- Per team per month: cost, headcount and cost per head
CREATE OR REPLACE VIEW v_team_monthly AS
WITH months AS (SELECT DISTINCT month FROM cost_team_monthly),
headcount AS (
    -- employees on the books on the 15th of each month
    SELECT m.month, u.team, count(*) AS headcount
    FROM months m JOIN users u
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
       round(sum(c.cost) FILTER (WHERE c.cost_type = 'user_activity') / nullif(h.headcount, 0), 2)
           AS user_cost_per_head
FROM cost_team_monthly c
LEFT JOIN headcount h USING (month, team)
GROUP BY c.month, c.team, h.headcount;

-- Company-level KPIs per month (Executive Overview)
CREATE OR REPLACE VIEW v_kpi_monthly AS
WITH bill AS (
    SELECT date_trunc('month', ChargePeriodStart)::DATE AS month,
           sum(EffectiveCost) AS total_cost,
           sum(ListCost) FILTER (WHERE ChargeCategory = 'Usage') AS list_cost
    FROM focus_cost GROUP BY ALL
),
classes AS (
    SELECT date_trunc('month', date)::DATE AS month,
           sum(cost) FILTER (WHERE cost_class = 'orphaned') AS orphaned_cost,
           sum(cost) FILTER (WHERE cost_class = 'unused_reservation') AS unused_reservation_cost,
           sum(cost) FILTER (WHERE cost_class = 'shared') AS shared_cost
    FROM cost_resource_daily GROUP BY ALL
),
idle AS (SELECT date_trunc('month', date)::DATE AS month, sum(cost) AS idle_cost FROM cost_idle GROUP BY ALL),
internal AS (
    SELECT month, count(DISTINCT user_id) AS active_employees, sum(total_cost) AS internal_user_cost
    FROM cost_user_monthly GROUP BY ALL
),
commerce AS (
    SELECT month,
           sum(transactions) FILTER (WHERE app_id = 'A12') AS orders,
           sum(revenue_usd) FILTER (WHERE app_id = 'A12') AS revenue_usd,
           sum(total_cost) FILTER (WHERE app_kind = 'customer') AS customer_app_cost,
           sum(total_cost) FILTER (WHERE app_id IN ('A12', 'A13')) AS order_path_cost
    FROM v_app_monthly GROUP BY ALL
)
SELECT b.month,
       round(b.total_cost, 2) AS total_cost,
       round(100 * (b.total_cost / lag(b.total_cost) OVER (ORDER BY b.month) - 1), 1) AS total_cost_mom_pct,
       round(b.list_cost - b.total_cost, 2) AS savings_vs_list,
       i.active_employees,
       round(i.internal_user_cost / i.active_employees, 2) AS cost_per_active_employee,
       cm.orders,
       round(cm.order_path_cost / cm.orders, 4) AS cost_per_order,
       round(cm.revenue_usd, 2) AS revenue_usd,
       round(100 * cm.customer_app_cost / cm.revenue_usd, 3) AS customer_infra_pct_of_revenue,
       round(coalesce(id.idle_cost, 0), 2) AS idle_cost,
       round(coalesce(c.orphaned_cost, 0), 2) AS orphaned_cost,
       round(coalesce(c.unused_reservation_cost, 0), 2) AS unused_reservation_cost,
       round(100 * (coalesce(id.idle_cost, 0) + coalesce(c.orphaned_cost, 0)
             + coalesce(c.unused_reservation_cost, 0)) / b.total_cost, 1) AS idle_unallocated_pct
FROM bill b
LEFT JOIN classes c USING (month)
LEFT JOIN idle id USING (month)
LEFT JOIN internal i USING (month)
LEFT JOIN commerce cm USING (month)
ORDER BY b.month;
