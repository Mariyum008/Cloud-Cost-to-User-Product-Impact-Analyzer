-- Step 3: direct cost per app per day, plus its share of shared platform cost (rule R3).
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
)
SELECT d.date,
       d.app_id,
       d.direct_cost,
       -- each app's slice of the day's shared cost = its share of the day's direct cost
       coalesce(s.shared_cost, 0) * d.direct_cost / sum(d.direct_cost) OVER (PARTITION BY d.date) AS shared_cost,
       d.direct_cost + coalesce(s.shared_cost, 0) * d.direct_cost
           / sum(d.direct_cost) OVER (PARTITION BY d.date) AS total_cost
FROM direct d
LEFT JOIN shared_pool s USING (date);
