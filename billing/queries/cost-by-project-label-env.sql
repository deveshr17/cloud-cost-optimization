-- Showback: monthly net cost by project, env, team and cost-center labels.
-- Unlabelled spend is surfaced explicitly so it can be chased down.
-- Labels are read from project.labels (org-wide tagging standard) and fall
-- back to resource labels when the project is unlabelled.

WITH labelled AS (
  SELECT
    invoice.month AS invoice_month,
    IFNULL(project.id, '(account)') AS project_id,
    COALESCE(
      (SELECT value FROM UNNEST(project.labels) WHERE key = 'env' LIMIT 1),
      (SELECT value FROM UNNEST(labels) WHERE key = 'env' LIMIT 1),
      '(unlabelled)'
    ) AS env,
    COALESCE(
      (SELECT value FROM UNNEST(project.labels) WHERE key = 'team' LIMIT 1),
      (SELECT value FROM UNNEST(labels) WHERE key = 'team' LIMIT 1),
      '(unlabelled)'
    ) AS team,
    COALESCE(
      (SELECT value FROM UNNEST(project.labels) WHERE key = 'cost-center' LIMIT 1),
      '(unlabelled)'
    ) AS cost_center,
    cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS net_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 3 MONTH), MONTH))
    AND cost_type = 'regular'
)

SELECT
  invoice_month,
  env,
  team,
  cost_center,
  project_id,
  ROUND(SUM(net_cost), 2) AS net_cost,
  ROUND(SAFE_DIVIDE(SUM(net_cost), SUM(SUM(net_cost)) OVER (PARTITION BY invoice_month)) * 100, 2) AS share_of_month_pct
FROM labelled
GROUP BY invoice_month, env, team, cost_center, project_id
HAVING net_cost <> 0
ORDER BY invoice_month DESC, net_cost DESC;
