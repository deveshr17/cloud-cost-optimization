-- Net cost by service for the trailing 30 days, with share of total and
-- month-over-month delta versus the preceding 30 days.
-- Table: detailed billing export (gcp_billing_export_resource_v1_*).
-- Placeholders: replace project/dataset/table before running.

DECLARE window_days INT64 DEFAULT 30;

WITH base AS (
  SELECT
    service.description AS service,
    DATE(usage_start_time) AS usage_date,
    cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS net_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 2 * window_days + 3 DAY))
    AND DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 * window_days DAY)
    AND cost_type = 'regular'
),

windows AS (
  SELECT
    service,
    SUM(IF(usage_date >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY), net_cost, 0)) AS current_30d,
    SUM(IF(usage_date < DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY), net_cost, 0)) AS previous_30d
  FROM base
  GROUP BY service
)

SELECT
  service,
  ROUND(current_30d, 2) AS current_30d,
  ROUND(previous_30d, 2) AS previous_30d,
  ROUND(current_30d - previous_30d, 2) AS delta,
  ROUND(SAFE_DIVIDE(current_30d - previous_30d, previous_30d) * 100, 1) AS delta_pct,
  ROUND(SAFE_DIVIDE(current_30d, SUM(current_30d) OVER ()) * 100, 1) AS share_pct
FROM windows
WHERE current_30d > 0 OR previous_30d > 0
ORDER BY current_30d DESC;
