-- Step 4: split session-based apps down to users (rules R1 and R2) and isolate idle cost.

-- Each session's weight
CREATE OR REPLACE TEMP TABLE sess AS
SELECT session_date::DATE AS date, date_trunc('month', session_date)::DATE AS month, app_id, resource_id, user_id,
       duration_hours AS hours,
       duration_hours * cpu_share_pct AS w
FROM sessions;

-- Daily cost of each AVD host VM
CREATE OR REPLACE TEMP TABLE host_cost AS
SELECT c.date, c.app_id, c.resource_id, c.cost
FROM cost_resource_daily c
JOIN apps a USING (app_id)
WHERE c.cost_class = 'direct' AND a.app_kind = 'avd'
  AND c.arm_type = 'microsoft.compute/virtualmachines';

-- Total weight per host-day and per app-month (the denominators of every split).
-- Hosts scale daily, so they split daily; the rest is always-on, so it splits over the month
-- (otherwise one person using HR Portal on a Sunday would carry that whole day's cost).
CREATE OR REPLACE TEMP TABLE host_w AS SELECT date, resource_id, sum(w) AS w_total FROM sess GROUP BY ALL;
CREATE OR REPLACE TEMP TABLE app_w AS SELECT month, app_id, sum(w) AS w_total FROM sess GROUP BY ALL;

-- App-month cost not tied to one host: disks, profiles, web/data/ML resources, and the shared slice
CREATE OR REPLACE TEMP TABLE app_pool AS
SELECT date_trunc('month', a.date)::DATE AS month, a.app_id,
       sum(a.direct_cost - coalesce(h.host_total, 0)) AS pool_direct,
       sum(a.shared_cost) AS shared_cost
FROM cost_app_daily a
LEFT JOIN (SELECT date, app_id, sum(cost) AS host_total FROM host_cost GROUP BY ALL) h USING (date, app_id)
WHERE a.app_id IN (SELECT DISTINCT app_id FROM sess)
GROUP BY ALL;

-- Every session's share of its host-day (R1), its app-month pool (R2) and the app's shared slice
CREATE OR REPLACE TABLE cost_user_monthly AS
WITH pieces AS (
    SELECT s.month, s.app_id, s.user_id, s.hours,
           coalesce(h.cost * s.w / hw.w_total, 0) AS host_part,
           p.pool_direct * s.w / aw.w_total AS pool_part,
           p.shared_cost * s.w / aw.w_total AS shared_part
    FROM sess s
    JOIN app_w aw USING (month, app_id)
    JOIN app_pool p USING (month, app_id)
    LEFT JOIN host_cost h ON h.date = s.date AND h.resource_id = s.resource_id
    LEFT JOIN host_w hw ON hw.date = s.date AND hw.resource_id = s.resource_id
)
SELECT p.month,
       p.user_id,
       u.team,
       p.app_id,
       count(*) AS sessions,
       round(sum(p.hours), 2) AS session_hours,
       sum(p.host_part + p.pool_part) AS direct_cost,
       sum(p.shared_part) AS shared_cost,
       sum(p.host_part + p.pool_part + p.shared_part) AS total_cost
FROM pieces p
JOIN users u USING (user_id)
GROUP BY ALL;

-- Idle: cost with nobody using it, kept with the app but never charged to users.
-- date = the day for idle hosts, or the 1st of the month for a month with no activity at all.
CREATE OR REPLACE TABLE cost_idle AS
SELECT h.date, h.app_id, 'idle_host' AS idle_type, sum(h.cost) AS cost
FROM host_cost h
ANTI JOIN host_w hw ON hw.date = h.date AND hw.resource_id = h.resource_id
GROUP BY ALL
UNION ALL
SELECT p.month, p.app_id, 'no_activity_month', p.pool_direct + p.shared_cost
FROM app_pool p
ANTI JOIN app_w aw ON aw.month = p.month AND aw.app_id = p.app_id;
