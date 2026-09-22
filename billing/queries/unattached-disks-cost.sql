-- Monthly cost of persistent disks that are NOT attached to any instance.
-- Billing data alone cannot tell attachment state, so this joins the export
-- against a Cloud Asset Inventory export (BigQuery destination) of
-- compute.googleapis.com/Disk resources. Set up the asset export with:
--   gcloud asset export --project=example-project-dev \
--     --content-type=resource --asset-types=compute.googleapis.com/Disk \
--     --bigquery-table=projects/example-project-billing/datasets/asset_inventory/tables/disks

DECLARE window_days INT64 DEFAULT 30;

WITH disk_cost AS (
  SELECT
    resource.global_name AS disk_global_name,
    resource.name AS disk_name,
    project.id AS project_id,
    location.zone AS zone,
    sku.description AS sku,
    SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net_cost_30d
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL window_days + 3 DAY))
    AND DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
    AND service.description = 'Compute Engine'
    AND REGEXP_CONTAINS(sku.description, r'(?i)PD Capacity|Hyperdisk|SSD backed PD')
    AND resource.global_name LIKE '//compute.googleapis.com/projects/%/disks/%'
  GROUP BY disk_global_name, disk_name, project_id, zone, sku
),

disk_state AS (
  -- Latest asset snapshot per disk; `users` is empty when unattached.
  SELECT
    name AS asset_name,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(resource.data, '$.users')) AS attached_count,
    JSON_VALUE(resource.data, '$.labels.keep') AS keep_label,
    TIMESTAMP(JSON_VALUE(resource.data, '$.lastDetachTimestamp')) AS last_detach
  FROM `example-project-billing.asset_inventory.disks`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY name ORDER BY update_time DESC) = 1
)

SELECT
  dc.project_id,
  dc.zone,
  dc.disk_name,
  dc.sku,
  ROUND(dc.net_cost_30d, 2) AS net_cost_30d,
  ds.last_detach,
  DATE_DIFF(CURRENT_DATE(), DATE(ds.last_detach), DAY) AS days_unattached,
  ds.keep_label = 'true' AS excluded_by_label
FROM disk_cost AS dc
INNER JOIN disk_state AS ds
  ON ds.asset_name = dc.disk_global_name
WHERE IFNULL(ds.attached_count, 0) = 0
ORDER BY net_cost_30d DESC;
