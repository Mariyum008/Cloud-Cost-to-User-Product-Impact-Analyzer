-- Phase 1 proof: naive vs usage-weighted cost per user for one AVD pool and month.
-- Change the app and month in params to explore.
WITH params AS (SELECT 'A02' AS app, DATE '2025-06-01' AS month_start),

-- Daily compute cost of each host
host_cost AS (
  SELECT f.ResourceId AS resource_id, f.ChargePeriodStart::DATE AS day, sum(f.EffectiveCost) AS cost
  FROM focus_cost f JOIN resources r ON f.ResourceId = r.resource_id, params p
  WHERE r.app_id = p.app AND r.arm_type = 'microsoft.compute/virtualmachines'
    AND date_trunc('month', f.ChargePeriodStart) = p.month_start
  GROUP BY 1, 2),

-- Each session's weight on its host that day: hours x CPU share
weights AS (
  SELECT s.user_id, s.resource_id, s.session_date::DATE AS day,
         s.duration_hours * s.cpu_share_pct AS w
  FROM sessions s, params p
  WHERE s.app_id = p.app AND date_trunc('month', s.session_date) = p.month_start),

-- Split each host-day's cost across its sessions by weight; host-days with no sessions stay idle
alloc AS (
  SELECT h.resource_id, h.day, h.cost, w.user_id,
         h.cost * w.w / sum(w.w) OVER (PARTITION BY h.resource_id, h.day) AS user_cost
  FROM host_cost h LEFT JOIN weights w USING (resource_id, day)),

per_user AS (
  SELECT user_id, sum(user_cost) AS weighted_cost FROM alloc WHERE user_id IS NOT NULL GROUP BY 1),

totals AS (
  SELECT (SELECT sum(cost) FROM host_cost) AS pool_cost,
         (SELECT sum(cost) FROM (SELECT DISTINCT resource_id, day, cost FROM alloc WHERE user_id IS NULL)) AS idle_cost,
         (SELECT count(*) FROM per_user) AS users)

SELECT round(pool_cost) AS pool_cost,
       -- idle = host-days the scaling plan kept on with no sessions
       round(coalesce(idle_cost, 0)) AS idle_host_cost,
       users,
       round(pool_cost / users, 2) AS naive_cost_per_user,
       (SELECT round(median(weighted_cost), 2) FROM per_user) AS weighted_median,
       (SELECT round(quantile_cont(weighted_cost, 0.1), 2) FROM per_user) AS weighted_p10,
       (SELECT round(quantile_cont(weighted_cost, 0.9), 2) FROM per_user) AS weighted_p90
FROM totals;
