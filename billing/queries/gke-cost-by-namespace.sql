-- GKE cost per cluster and Kubernetes namespace using GKE cost allocation labels.
-- Requires `--enable-cost-allocation` on the cluster; node cost is attributed
-- to namespaces proportionally to pod resource requests. Unallocated cost is
-- capacity that no pod requested (idle nodes, system overhead) and is the
-- first thing to look at for bin-packing improvements.

DECLARE window_days INT64 DEFAULT 30;

WITH gke AS (
  SELECT
    DATE(usage_start_time) AS usage_date,
    project.id AS project_id,
    (SELECT value FROM UNNEST(labels) WHERE key = 'goog-k8s-cluster-name' LIMIT 1) AS cluster,
    (SELECT value FROM UNNEST(labels) WHERE key = 'k8s-namespace' LIMIT 1) AS namespace,
    (SELECT value FROM UNNEST(labels) WHERE key = 'k8s-workload-name' LIMIT 1) AS workload,
    CASE
      WHEN REGEXP_CONTAINS(sku.description, r'(?i)spot|preemptible') THEN 'spot'
      ELSE 'on-demand'
    END AS pricing_model,
    cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0) AS net_cost
  FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`
  WHERE
    _PARTITIONTIME >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL window_days + 3 DAY))
    AND DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
    AND service.description IN ('Compute Engine', 'Kubernetes Engine')
    AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'goog-k8s-cluster-name')
    AND cost_type = 'regular'
)

SELECT
  project_id,
  cluster,
  IFNULL(namespace, '(unallocated)') AS namespace,
  pricing_model,
  ROUND(SUM(net_cost), 2) AS net_cost_30d,
  ROUND(SUM(net_cost) / window_days, 2) AS avg_daily_cost,
  ROUND(SAFE_DIVIDE(SUM(net_cost), SUM(SUM(net_cost)) OVER (PARTITION BY project_id, cluster)) * 100, 1) AS share_of_cluster_pct
FROM gke
GROUP BY project_id, cluster, namespace, pricing_model
ORDER BY project_id, cluster, net_cost_30d DESC;
