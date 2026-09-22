-- Network egress cost by destination class for the trailing 30 days.
-- Separates internet egress (most expensive and most often avoidable via
-- Cloud CDN / caching) from inter-region and inter-zone traffic (usually a
-- topology decision: multi-zone GKE clusters, cross-region replicas).

DECLARE window_days INT64 DEFAULT 30;

WITH egress AS (
  SELECT
    DATE(usage_start_time) AS usage_date,
    project.id AS project_id,
    service.description AS service,
    sku.description AS sku,
    CASE
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)internet|to (china|australia|americas|emea|apac)') THEN 'internet'
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)inter-?region|inter region') THEN 'inter-region'
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)inter-?zone|inter zone') THEN 'inter-zone'
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)cdn') THEN 'cdn-cache-egress'
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)interconnect|vpn') THEN 'hybrid'
      ELSE 'other-network'
    END AS egress_class,
    usage.amount_in_pricing_units AS gib,
    cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS net_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL window_days + 3 DAY))
    AND DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
    AND REGEXP_CONTAINS(sku.description, r'(?i)egress|network.*(internet|inter)')
    AND usage.pricing_unit IN ('gibibyte', 'gigabyte')
    AND cost_type = 'regular'
)

SELECT
  project_id,
  egress_class,
  service,
  ROUND(SUM(gib), 1) AS gib_30d,
  ROUND(SUM(net_cost), 2) AS net_cost_30d,
  ROUND(SAFE_DIVIDE(SUM(net_cost), SUM(gib)), 4) AS effective_price_per_gib,
  ROUND(SAFE_DIVIDE(SUM(net_cost), SUM(SUM(net_cost)) OVER ()) * 100, 1) AS share_pct
FROM egress
GROUP BY project_id, egress_class, service
HAVING net_cost_30d > 1
ORDER BY net_cost_30d DESC;
