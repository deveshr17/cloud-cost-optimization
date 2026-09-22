# Enabling detailed billing export to BigQuery

Every query in `queries/` targets the **detailed usage cost** export table
(`gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`). The standard export
lacks `resource.name` and `resource.global_name`, which are what let you tie
a cost line back to a specific disk, bucket or GKE node. Enable the detailed
export even if the standard one already exists.

## 1. Create the destination dataset

```bash
export PROJECT_ID=example-project-billing
export DATASET=billing_export
export LOCATION=asia-south1

bq --location="${LOCATION}" mk \
  --dataset \
  --description "Cloud Billing detailed export" \
  --default_table_expiration 0 \
  "${PROJECT_ID}:${DATASET}"
```

Recommendations for the dataset:

| Setting | Value | Why |
| --- | --- | --- |
| Location | Same region as your analytics (e.g. `asia-south1`) | Avoid cross-region query costs |
| Table expiration | none | Billing history is the asset; keep it |
| CMEK | optional | Only if org policy requires |
| Access | `roles/bigquery.dataViewer` to a `finops-readers` group | Least privilege; the export SA needs `roles/bigquery.dataEditor` on the dataset |

## 2. Enable the export (Console only)

Cloud Billing export cannot be enabled with Terraform or `gcloud`; it is a
billing-account-level setting.

1. Console: **Billing -> Billing export -> BigQuery export**.
2. Under **Detailed usage cost**, choose *Edit settings*, pick the project and dataset above, and save.
3. Optionally enable **Pricing** export as well (`cloud_pricing_export`), which the CUD coverage query joins against.

The first partition appears within a few hours; historical data is **not**
backfilled, so enable this on day one of any FinOps engagement.

## 3. Verify

```sql
SELECT
  MIN(usage_start_time) AS first_row,
  MAX(export_time)      AS latest_export,
  COUNT(*)              AS rows_total
FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`;
```

## 4. Enable GKE cost allocation

To populate the `k8s-namespace` / `k8s-workload` labels used by
`queries/gke-cost-by-namespace.sql`, turn on cost allocation on each cluster
(also set in `terraform/gke-cost-controls/main.tf`):

```bash
gcloud container clusters update gke-dev-primary \
  --region asia-south1 \
  --enable-cost-allocation
```

Labels appear in the export as `labels.key = 'k8s-namespace'` on
Compute Engine node SKUs, allocated proportionally to pod requests.

## 5. Views for dashboards

A thin view hides the billing account ID in the table name and pre-computes
the labels most dashboards need:

```sql
CREATE OR REPLACE VIEW `example-project-billing.billing_export.v_cost_daily` AS
SELECT
  DATE(usage_start_time, 'Asia/Kolkata')                                            AS usage_date,
  project.id                                                                          AS project_id,
  service.description                                                                 AS service,
  sku.description                                                                     AS sku,
  location.region                                                                     AS region,
  (SELECT value FROM UNNEST(project.labels) WHERE key = 'env'          LIMIT 1)      AS env,
  (SELECT value FROM UNNEST(project.labels) WHERE key = 'team'         LIMIT 1)      AS team,
  (SELECT value FROM UNNEST(project.labels) WHERE key = 'cost-center'  LIMIT 1)      AS cost_center,
  (SELECT value FROM UNNEST(labels)         WHERE key = 'app'          LIMIT 1)      AS app,
  (SELECT value FROM UNNEST(labels)         WHERE key = 'k8s-namespace' LIMIT 1)     AS k8s_namespace,
  cost,
  (SELECT SUM(c.amount) FROM UNNEST(credits) c)                                       AS credits,
  currency
FROM `example-project-billing.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01`;
```

Grant dashboard service accounts access to the view, not the base table.

## Cost of the export itself

The export table is partitioned by `_PARTITIONTIME`; queries in this repo
always filter on it. Expect a few GB per month for a mid-sized organisation;
storage converts to long-term pricing after 90 days automatically.
