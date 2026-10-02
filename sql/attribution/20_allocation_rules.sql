-- Step 2: the rules behind every split, written out with this run's settings so results are explainable.
CREATE OR REPLACE TABLE allocation_rules AS
WITH s AS (SELECT
    (SELECT value FROM cfg_settings WHERE key = 'session_weight') AS weight,
    (SELECT value FROM cfg_settings WHERE key = 'shared_split') AS shared)
SELECT * FROM (
    SELECT 'R1' AS rule_id, 'Apps split by host (desktops, VMs users log into)' AS applies_to, 'host-day' AS grain,
           'session ' || replace(s.weight, '_x_', ' x ') || ' on that host that day' AS driver,
           'Users pay for the machine they actually used. Host-days with no sessions become idle cost.' AS rationale FROM s
    UNION ALL SELECT 'R2', 'Apps split by sessions, and non-host cost of R1 apps', 'app-month',
           'session ' || replace(s.weight, '_x_', ' x ') || ' across the app''s sessions that month',
           'Always-on cost is split over the month so a lone weekend user does not absorb a whole day.' FROM s
    UNION ALL SELECT 'R3', 'Shared platform apps', 'day',
           CASE s.shared WHEN 'even' THEN 'equal share per app with cost that day' ELSE 'each app''s share of that day''s direct cost' END,
           'Platform services exist for every app. The resulting slice then flows to users through R2.' FROM s
    UNION ALL SELECT 'R4', 'Apps split by owner, or session apps without session data', 'app',
           'owner team', 'Charged to the owning team; unit cost is measured per transaction instead of per person.' FROM s
    UNION ALL SELECT 'R5', 'Orphaned, unused reservation, unmapped and other charges', 'resource-day',
           'not allocated', 'Kept visible instead of being hidden inside other teams'' costs.' FROM s
);
