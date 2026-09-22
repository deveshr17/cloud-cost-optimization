# BigQuery optimization

BigQuery bills for two things: bytes scanned (on-demand) or slot time
(editions), and storage. `detectors/bigquery_optimizer.py` covers both.

## Compute

### On-demand vs editions

| Model | Billed on | Best for |
| --- | --- | --- |
| On-demand | Bytes processed (per TiB) | Bursty, unpredictable, < ~200 slot-hours/day |
| Standard edition | Slot-hours, autoscaled in 100-slot steps | Steady dashboards + ELT, no BI Engine / CMEK needs |
| Enterprise / Enterprise Plus | Slot-hours, optional 1y/3y commitments | Large steady workloads; commitment discount |

The tool computes average concurrent slots from `INFORMATION_SCHEMA.JOBS`
slot-ms and compares extrapolated on-demand spend to the smallest 100-slot
baseline. Rule of thumb: if on-demand is > 1.2x the reservation, move; if
< 0.8x, stay; in between, cut bytes first.

Hybrid is normal: a reservation for the ETL project, on-demand for ad-hoc
analysts, assigned per project with `assignments`.

### Reduce bytes scanned

1. **Partition** on the date column every query filters by; set `require_partition_filter = TRUE` so an unfiltered `SELECT *` fails instead of costing money.
2. **Cluster** on the next 1-4 filter/join columns (`tenant_id`, `service`).
3. **Materialise** repeated queries: a dashboard hitting a raw table 96 times a day should read a scheduled summary table or a materialized view.
4. **BI Engine** for dashboards: in-memory acceleration priced per GB reserved, often cheaper than the scans it replaces.
5. **Never `SELECT *`** on wide tables; BigQuery is columnar, so column pruning is the cheapest optimisation available.
6. **Maximum bytes billed** on the job (`--maximum_bytes_billed`) as a per-query circuit breaker for analysts.

### Finding the offenders

```sql
SELECT user_email, COUNT(*) runs, ROUND(SUM(total_bytes_billed)/POW(1024,4),2) tib
FROM `region-asia-south1`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) AND job_type='QUERY'
GROUP BY 1 ORDER BY tib DESC LIMIT 20;
```

Label jobs (`--label team=growth`) so this is attributable; the detector
reads `label_team`.

## Storage

- **Active vs long-term**: partitions untouched for 90 days drop to roughly half the storage price automatically. An unpartitioned table rewritten daily never qualifies; partition it.
- **Partition expiration**: `ALTER TABLE ... SET OPTIONS (partition_expiration_days = 180)` for logs and staging. Agree the retention window with the owner and write it in the table description.
- **Physical vs logical billing**: for highly compressible data (logs, JSON) switch the dataset to `PHYSICAL` storage billing and compare with `TABLE_STORAGE`'s `total_physical_bytes`. Time travel window (`max_time_travel_hours`) is billed under physical; reduce it from 7 days to 2 for staging datasets.
- **Snapshots and clones** cost only the delta; use them instead of `CREATE TABLE AS` copies.

## Guardrails

- Custom quota: `Query usage per day per user` at the project level stops one runaway analyst.
- `require_partition_filter` on every table over 1 TB.
- Dataset-level default table expiration for `staging` and `scratch` datasets.
- Alert on `bigquery.googleapis.com/query/scanned_bytes_billed` daily sum (see `dashboards/cloud-monitoring-dashboard.json`).
