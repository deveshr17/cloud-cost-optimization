-- Daily cost anomaly detection per project and service using a z-score
-- against the trailing 30-day baseline (excluding the 2 most recent days,
-- which are still being restated by late-arriving billing rows).
-- Rows with |z| >= 3 and an absolute delta above a floor are anomalies.
-- Schedule this query daily and route rows to Pub/Sub -> Slack.

DECLARE baseline_days INT64 DEFAULT 30;
DECLARE settle_days INT64 DEFAULT 2;
DECLARE z_threshold FLOAT64 DEFAULT 3.0;
DECLARE min_abs_delta FLOAT64 DEFAULT 50.0;  -- currency units; avoids alerting on tiny services

WITH daily AS (
  SELECT
    DATE(usage_start_time) AS usage_date,
    IFNULL(project.id, '(account)') AS project_id,
    service.description AS service,
    SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL baseline_days + settle_days + 5 DAY))
    AND DATE(usage_start_time) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL baseline_days + settle_days + 1 DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL settle_days DAY)
    AND cost_type = 'regular'
  GROUP BY usage_date, project_id, service
),

-- Dense calendar so days with zero spend count as 0 rather than being skipped.
calendar AS (
  SELECT usage_date
  FROM UNNEST(GENERATE_DATE_ARRAY(
    DATE_SUB(CURRENT_DATE(), INTERVAL baseline_days + settle_days + 1 DAY),
    DATE_SUB(CURRENT_DATE(), INTERVAL settle_days DAY)
  )) AS usage_date
),

series AS (
  SELECT
    c.usage_date,
    k.project_id,
    k.service,
    IFNULL(d.net_cost, 0) AS net_cost
  FROM calendar AS c
  CROSS JOIN (SELECT DISTINCT project_id, service FROM daily) AS k
  LEFT JOIN daily AS d
    ON d.usage_date = c.usage_date AND d.project_id = k.project_id AND d.service = k.service
),

scored AS (
  SELECT
    usage_date,
    project_id,
    service,
    net_cost,
    AVG(net_cost) OVER w AS baseline_mean,
    STDDEV_SAMP(net_cost) OVER w AS baseline_stddev,
    COUNT(net_cost) OVER w AS baseline_n
  FROM series
  WINDOW w AS (
    PARTITION BY project_id, service
    ORDER BY UNIX_DATE(usage_date)
    RANGE BETWEEN 30 PRECEDING AND 1 PRECEDING
  )
)

SELECT
  usage_date,
  project_id,
  service,
  ROUND(net_cost, 2) AS net_cost,
  ROUND(baseline_mean, 2) AS baseline_mean,
  ROUND(baseline_stddev, 2) AS baseline_stddev,
  ROUND(SAFE_DIVIDE(net_cost - baseline_mean, NULLIF(baseline_stddev, 0)), 2) AS z_score,
  ROUND(net_cost - baseline_mean, 2) AS abs_delta,
  IF(net_cost > baseline_mean, 'spike', 'drop') AS direction
FROM scored
WHERE
  usage_date = DATE_SUB(CURRENT_DATE(), INTERVAL settle_days DAY)
  AND baseline_n >= 14
  AND ABS(SAFE_DIVIDE(net_cost - baseline_mean, NULLIF(baseline_stddev, 0))) >= z_threshold
  AND ABS(net_cost - baseline_mean) >= min_abs_delta
ORDER BY ABS(net_cost - baseline_mean) DESC;
