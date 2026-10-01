-- Join integrity and shape checks. Run: duckdb data/synthetic/cloud_impact.duckdb < sql/sanity_checks.sql

-- Every session points at a resource in the inventory (expect 0)
SELECT 'sessions without resource' AS check_name, count(*) AS bad
FROM sessions s ANTI JOIN resources r ON s.resource_id = r.resource_id;

-- Every usage cost row maps to the inventory, except reservation rows (expect 0)
SELECT 'cost rows without resource', count(*)
FROM focus_cost f ANTI JOIN resources r ON f.ResourceId = r.resource_id
WHERE f.ResourceType <> 'Reservation';

-- Untagged spend: the attribution gap the engine must close via the inventory join
SELECT 'untagged share of cost (%)',
       round(100 * sum(EffectiveCost) FILTER (WHERE Tags = '{}') / sum(EffectiveCost), 2)
FROM focus_cost;

-- Monthly cost vs activity per app
SELECT b.app_id, strftime(b.date, '%Y-%m') AS month,
       sum(b.active_users) AS user_days, sum(b.transactions) AS transactions, round(c.cost) AS effective_cost
FROM business_metrics b
JOIN (SELECT r.app_id, date_trunc('month', f.ChargePeriodStart) AS m, sum(f.EffectiveCost) AS cost
      FROM focus_cost f JOIN resources r ON f.ResourceId = r.resource_id GROUP BY 1, 2) c
  ON c.app_id = b.app_id AND c.m = date_trunc('month', b.date)
GROUP BY b.app_id, month, c.cost
ORDER BY b.app_id, month
LIMIT 24;
