-- Step 3: direct cost per app per day, plus its slice of shared platform cost (rule R3).
CREATE OR REPLACE TABLE cost_app_daily AS
WITH direct AS (
    SELECT date, app_id, sum(cost) AS direct_cost
    FROM cost_resource_daily
    WHERE cost_class = 'direct'
    GROUP BY ALL
),
shared_pool AS (
    SELECT date, sum(cost) AS shared_cost
    FROM cost_resource_daily
    WHERE cost_class = 'shared'
    GROUP BY ALL
),
split AS (
    SELECT d.*, coalesce(s.shared_cost, 0) *
           CASE (SELECT value FROM cfg_settings WHERE key = 'shared_split')
               WHEN 'even' THEN 1.0 / count(*) OVER (PARTITION BY d.date)
               ELSE d.direct_cost / nullif(sum(d.direct_cost) OVER (PARTITION BY d.date), 0)
           END AS shared_cost
    FROM direct d
    LEFT JOIN shared_pool s USING (date)
)
SELECT date, app_id, direct_cost, coalesce(shared_cost, 0) AS shared_cost, direct_cost + coalesce(shared_cost, 0) AS total_cost
FROM split;
