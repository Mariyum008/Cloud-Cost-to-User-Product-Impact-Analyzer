-- Step 5: every dollar of the bill, per team per month, labelled by how it got there.
CREATE OR REPLACE TABLE cost_team_monthly AS
-- Users' attributed cost goes to their own team (R1, R2)
SELECT month, team, app_id, 'user_activity' AS cost_type, sum(total_cost) AS cost
FROM cost_user_monthly
GROUP BY ALL
UNION ALL
-- Idle capacity is charged to the team that owns the app
SELECT date_trunc('month', i.date)::DATE, a.owner_team, i.app_id, 'idle_capacity', sum(i.cost)
FROM cost_idle i JOIN apps a USING (app_id)
GROUP BY ALL
UNION ALL
-- Apps without employee sessions are charged to their owner team (R4)
SELECT date_trunc('month', c.date)::DATE, a.owner_team, c.app_id, 'owned_app', sum(c.total_cost)
FROM cost_app_daily c JOIN apps a USING (app_id)
WHERE c.app_id NOT IN (SELECT DISTINCT app_id FROM sessions)
GROUP BY ALL
UNION ALL
-- Waste that no team should silently absorb (R5)
SELECT date_trunc('month', date)::DATE, 'Unallocated', NULL, cost_class, sum(cost)
FROM cost_resource_daily
WHERE cost_class IN ('orphaned', 'unused_reservation')
GROUP BY ALL;
