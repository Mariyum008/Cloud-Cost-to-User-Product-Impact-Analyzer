-- Step 1: daily effective cost per resource, classified by who should carry it.
--   direct             -> belongs to one app
--   shared             -> belongs to a shared platform app (split across apps in step 3)
--   orphaned           -> disk or public IP whose VM is already deleted (needs an inventory)
--   unused_reservation -> prepaid capacity nobody used
--   unmapped           -> no inventory entry or mapping rule matched it
--   other_charges      -> tax, credits, adjustments and other non-usage charges
CREATE OR REPLACE TABLE cost_resource_daily AS
WITH charges AS (
    -- Purchases carry no EffectiveCost (it's already spread over usage rows), so they add nothing here
    SELECT ChargePeriodStart::DATE AS date,
           ResourceId AS resource_id,
           ChargeCategory = 'Usage' AS is_usage,
           coalesce(CommitmentDiscountStatus = 'Unused', false) AS is_unused,
           sum(EffectiveCost) AS cost
    FROM in_cost
    GROUP BY ALL
),
vm_life AS (
    -- When each VM was deleted, matched to its disk/IP by name
    SELECT app_id, resource_name AS vm_name, deleted_date
    FROM resource_dim
    WHERE is_vm AND deleted_date IS NOT NULL
)
SELECT c.date,
       c.resource_id,
       CASE WHEN c.is_unused OR NOT c.is_usage THEN NULL ELSE d.app_id END AS app_id,
       d.arm_type,
       c.cost,
       CASE
           WHEN NOT c.is_usage THEN 'other_charges'
           WHEN c.is_unused THEN 'unused_reservation'
           WHEN v.deleted_date IS NOT NULL AND c.date >= v.deleted_date THEN 'orphaned'
           WHEN d.app_id IS NULL THEN 'unmapped'
           WHEN a.role = 'shared' THEN 'shared'
           ELSE 'direct'
       END AS cost_class
FROM charges c
LEFT JOIN resource_dim d ON d.resource_id = c.resource_id
LEFT JOIN cfg_apps a ON a.app_id = d.app_id
LEFT JOIN vm_life v
       ON v.app_id = d.app_id
      AND d.arm_type IN ('microsoft.compute/disks', 'microsoft.network/publicipaddresses')
      AND v.vm_name = regexp_replace(regexp_replace(d.resource_name, '-osdisk$', ''), '^pip-', '');
