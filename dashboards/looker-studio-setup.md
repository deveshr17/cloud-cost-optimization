# Looker Studio cost dashboard

Looker Studio (free) on top of the billing export is the fastest way to give
finance and team leads a self-serve view without granting BigQuery console
access.

## Data source

1. In BigQuery, create the `v_cost_daily` view from
   `billing/bigquery-export-setup.md` (it hides the billing-account-ID table
   name and flattens labels).
2. In Looker Studio: **Create -> Data source -> BigQuery -> Custom query** and
   use the view directly, or point at the view with **Date range dimension**
   set to `usage_date`. Custom query variant that keeps the scan partitioned:

   ```sql
   SELECT *
   FROM `example-project-billing.billing_export.v_cost_daily`
   WHERE usage_date BETWEEN PARSE_DATE('%Y%m%d', @DS_START_DATE)
                        AND PARSE_DATE('%Y%m%d', @DS_END_DATE)
   ```

   Enable **Date range parameters** so `@DS_START_DATE`/`@DS_END_DATE` are
   injected. This keeps every dashboard load pruned by partition.
3. Data credentials: **Owner's credentials**, using a dedicated
   `finops-looker@…` service account with `roles/bigquery.jobUser` on the
   billing project and `roles/bigquery.dataViewer` on the view (authorised
   view; no access to the base table).

## Pages

| Page | Charts | Filters |
| --- | --- | --- |
| Overview | Scorecards: MTD net cost, forecast (MTD / days elapsed * days in month), MoM %, credits; time series by service | env, team |
| Showback | Pivot: team x env with cost-center; bar: top 10 projects; table: unlabelled cost with project IDs | invoice month |
| GKE | Stacked bar by `k8s_namespace` per cluster; "(unallocated)" share as a gauge | cluster |
| Commitments | CUD coverage % (from `committed-use-coverage.sql` as a second source), SUD trend | region |
| Anomalies | Table fed by a scheduled query writing `daily-anomaly-detection.sql` output to `finops.anomalies` | date |

## Calculated fields

```text
net_cost         = cost + IFNULL(credits, 0)
mtd_forecast     = SUM(net_cost) / DAY(TODAY()) * DAY(LAST_DAY(TODAY()))
unlabelled_flag  = CASE WHEN team IS NULL OR team = '(unlabelled)' THEN 'unlabelled' ELSE 'labelled' END
```

## Refresh and cost of the dashboard itself

- Data freshness: 12 hours (billing export lands a few times per day).
- Enable **BI Engine** (1 GB reservation) on the billing dataset if the
  report has more than a handful of daily viewers; the pricing is per
  reserved GB and is usually cheaper than the repeated scans.
- Every widget is one query; keep the default date range to 30 days and
  avoid table charts with `sku` granularity on the overview page.

## Sharing

Share the report with a Google Group (`finops-readers@example.com`); do not
grant the underlying dataset to individuals. Schedule a weekly PDF email to
team leads from **Share -> Schedule email delivery**.
