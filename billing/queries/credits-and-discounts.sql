-- Breakdown of every credit type applied in the trailing 3 invoice months.
-- Answers: how much of the bill is list price vs what we pay, where SUD
-- and CUD land, and whether promotional credits are about to run out.

WITH credit_lines AS (
  SELECT
    invoice.month AS invoice_month,
    service.description AS service,
    c.type AS credit_type,
    c.name AS credit_name,
    c.amount AS credit_amount,
    cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  LEFT JOIN UNNEST(credits) AS c
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 3 MONTH), MONTH))
    AND cost_type = 'regular'
),

per_type AS (
  SELECT
    invoice_month,
    service,
    IFNULL(credit_type, 'NO_CREDIT') AS credit_type,
    SUM(IF(credit_type IS NULL, cost, 0)) AS list_cost_without_credit,
    SUM(IFNULL(credit_amount, 0)) AS credit_total
  FROM credit_lines
  GROUP BY invoice_month, service, credit_type
),

totals AS (
  -- Rows are duplicated per credit by the LEFT JOIN UNNEST above, so list
  -- cost is taken from the base table directly.
  SELECT
    invoice.month AS invoice_month,
    SUM(cost) AS list_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 3 MONTH), MONTH))
    AND cost_type = 'regular'
  GROUP BY invoice_month
)

SELECT
  p.invoice_month,
  p.service,
  p.credit_type,
  ROUND(-p.credit_total, 2) AS credit_value,
  ROUND(SAFE_DIVIDE(-p.credit_total, t.list_cost) * 100, 2) AS pct_of_month_list_cost,
  CASE p.credit_type
    WHEN 'COMMITTED_USAGE_DISCOUNT' THEN 'CUD - resource based; check coverage query'
    WHEN 'COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE' THEN 'CUD - spend based (Cloud SQL, GKE Autopilot, etc.)'
    WHEN 'SUSTAINED_USAGE_DISCOUNT' THEN 'SUD - automatic; disappears when VMs move to CUD or E2/T2D'
    WHEN 'PROMOTION' THEN 'Promotional credit; confirm expiry date with account team'
    WHEN 'FREE_TIER' THEN 'Always-free allowance'
    WHEN 'SPENDING_BASED_DISCOUNT' THEN 'Negotiated volume discount'
    WHEN 'DISCOUNT' THEN 'Contractual discount'
    ELSE 'n/a'
  END AS meaning
FROM per_type AS p
INNER JOIN totals AS t
  ON t.invoice_month = p.invoice_month
WHERE p.credit_type <> 'NO_CREDIT'
ORDER BY p.invoice_month DESC, credit_value DESC;
