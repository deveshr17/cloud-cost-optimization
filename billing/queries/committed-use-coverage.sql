-- Committed use discount (CUD) coverage and utilisation for Compute Engine.
-- Coverage  = share of eligible on-demand vCPU/RAM spend that received a CUD credit.
-- Idle CUD  = commitment fee lines without matching usage (paying for unused commitment).
-- Interpretation guide: coverage < 60% with stable baseline -> buy more;
-- idle_cud_cost > 0 -> over-committed or wrong region/family.

DECLARE window_days INT64 DEFAULT 30;

WITH lines AS (
  SELECT
    location.region AS region,
    (SELECT value FROM UNNEST(system_labels) WHERE key = 'compute.googleapis.com/machine_spec' LIMIT 1) AS machine_spec,
    sku.description AS sku,
    cost,
    IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c WHERE c.type IN ('COMMITTED_USAGE_DISCOUNT', 'COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE')), 0) AS cud_credit,
    IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c WHERE c.type = 'SUSTAINED_USAGE_DISCOUNT'), 0) AS sud_credit,
    REGEXP_CONTAINS(sku.description, r'^Commitment v\d+:') AS is_commitment_fee,
    REGEXP_CONTAINS(sku.description, r'(?i)(core|ram|instance)') AND NOT REGEXP_CONTAINS(sku.description, r'(?i)spot|preemptible|commitment') AS is_eligible_usage
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL window_days + 3 DAY))
    AND DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
    AND service.description = 'Compute Engine'
    AND cost_type = 'regular'
)

SELECT
  region,
  SPLIT(IFNULL(machine_spec, 'unknown'), '-')[SAFE_OFFSET(0)] AS machine_family,
  ROUND(SUM(IF(is_eligible_usage, cost, 0)), 2) AS eligible_on_demand_cost,
  ROUND(-SUM(IF(is_eligible_usage, cud_credit, 0)), 2) AS cud_credit_applied,
  ROUND(-SUM(IF(is_eligible_usage, sud_credit, 0)), 2) AS sud_credit_applied,
  ROUND(SUM(IF(is_commitment_fee, cost, 0)), 2) AS commitment_fees,
  ROUND(SAFE_DIVIDE(-SUM(IF(is_eligible_usage, cud_credit, 0)), SUM(IF(is_eligible_usage, cost, 0))) * 100, 1) AS cud_coverage_pct,
  -- Fee paid for commitment minus discount actually applied approximates idle commitment.
  ROUND(GREATEST(SUM(IF(is_commitment_fee, cost, 0)) + SUM(IF(is_eligible_usage, cud_credit, 0)), 0), 2) AS idle_cud_cost
FROM lines
GROUP BY region, machine_family
HAVING eligible_on_demand_cost > 0 OR commitment_fees > 0
ORDER BY eligible_on_demand_cost DESC;
