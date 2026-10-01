-- Step 2: the written-down rules, so every split in the engine is explainable.
CREATE OR REPLACE TABLE allocation_rules AS
SELECT * FROM (VALUES
    ('R1', 'AVD host VMs', 'host-day', 'session_hours x cpu_share_pct of sessions on that host that day',
     'Heavier, longer sessions pay more of the host they ran on. Host-days with no sessions become idle cost.'),
    ('R2', 'Other app cost (web, data, ML, AVD disks and profiles)', 'app-month',
     'session_hours x cpu_share_pct of all the app''s sessions that month',
     'Always-on cost is split over the month so a lone weekend user does not absorb a whole day. Months with cost but no sessions become idle cost.'),
    ('R3', 'Shared platform (Backup & DR, networking, logging, Key Vault)', 'day',
     'each app''s share of that day''s direct cost',
     'Proportional allocation: bigger apps lean on shared services more. Then split to users by R2.'),
    ('R4', 'Customer-facing apps, ETL, Dev/Test', 'app', 'owner team (no employee sessions)',
     'Charged to the owning team; unit cost is measured per transaction or pipeline run instead of per user.'),
    ('R5', 'Orphaned disks/IPs and unused reservation hours', 'resource-day', 'not allocated',
     'Kept visible as waste instead of being hidden inside other teams'' costs.')
) AS t(rule_id, applies_to, grain, driver, rationale);
