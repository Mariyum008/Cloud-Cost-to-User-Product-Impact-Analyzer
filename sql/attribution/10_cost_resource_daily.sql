-- Step 1: daily effective cost per resource, classified by who should carry it.
--   direct             -> belongs to one app (most cost)
--   shared             -> platform services used by every app (split in step 3)
--   orphaned           -> disk or public IP whose VM is already deleted (waste, not charged to users)
--   unused_reservation -> prepaid capacity nobody used (waste, not charged to users)
CREATE OR REPLACE TABLE cost_resource_daily AS
WITH usage AS (
    -- Purchase rows are skipped: their EffectiveCost is 0 because it is already spread over usage rows
    SELECT ChargePeriodStart::DATE AS date,
           ResourceId AS resource_id,
           coalesce(CommitmentDiscountStatus = 'Unused', false) AS is_unused,
           sum(EffectiveCost) AS cost
    FROM focus_cost
    WHERE ChargeCategory = 'Usage'
    GROUP BY ALL
),
vm_life AS (
    -- When each VM was deleted, matched to its disk/IP by name
    SELECT app_id, resource_name AS vm_name, deleted_date
    FROM resources
    WHERE arm_type = 'microsoft.compute/virtualmachines'
)
SELECT u.date,
       u.resource_id,
       r.app_id,
       r.arm_type,
       u.cost,
       CASE
           WHEN u.is_unused THEN 'unused_reservation'
           WHEN v.deleted_date IS NOT NULL AND u.date >= v.deleted_date THEN 'orphaned'
           WHEN a.app_kind = 'platform' THEN 'shared'
           ELSE 'direct'
       END AS cost_class
FROM usage u
LEFT JOIN resources r ON u.resource_id = r.resource_id
LEFT JOIN apps a ON r.app_id = a.app_id
LEFT JOIN vm_life v
       ON v.app_id = r.app_id
      AND r.arm_type IN ('microsoft.compute/disks', 'microsoft.network/publicipaddresses')
      AND v.vm_name = regexp_replace(regexp_replace(r.resource_name, '-osdisk$', ''), '^pip-', '');
