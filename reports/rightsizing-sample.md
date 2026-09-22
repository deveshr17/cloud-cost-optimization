# Rightsizing report: gke-dev-primary (14d lookback)

Generated: 2026-09-21 02:00 UTC  
Source: `detectors/fixtures/prometheus_usage.json`

> All cost figures are **estimates computed from placeholder unit prices on synthetic data**. They illustrate the calculation method and are not realised savings.

## Summary

| Metric | Value |
| --- | --- |
| Containers analysed | 8 |
| Over-provisioned | 4 |
| Under-provisioned (OOM, throttling or usage above request) | 3 |
| Within tolerance | 1 |
| Requested CPU that can be released (cores) | 11.85 |
| Requested memory that can be released (GiB) | 26.50 |
| Estimated monthly reduction (requests, placeholder prices) | 268.91 USD |
| Estimated monthly increase for under-provisioned fixes | 80.95 USD |
| CPU limits policy | none |

## Recommendations

| Workload | Kind | Container | Replicas | CPU request | Memory request | CPU p95 / p99 | Mem p99 | VPA target | Status | Monthly delta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| data/nightly-etl | CronJob | etl | 1 | 4000m -> **4700m** | 16384Mi -> **15360Mi** | 3600m / 3900m | 12800Mi | - | UNDER | 10.03 USD |
| search/query-api | Deployment | app | 8 | 250m -> **350m** | 512Mi -> **608Mi** | 240m / 290m | 500Mi | 280m / 560Mi | UNDER | 15.88 USD |
| payments/ledger-worker | Deployment | worker | 4 | 500m -> **1250m** | 1024Mi -> **1248Mi** | 780m / 940m | 980Mi | 950m / 1180Mi | UNDER | 55.04 USD |
| payments/checkout-api | Deployment | app | 6 | 1000m -> **300m** | 2048Mi -> **832Mi** | 210m / 310m | 680Mi | 260m / 720Mi | OVER | -90.84 USD |
| search/indexer | StatefulSet | indexer | 3 | 2000m -> **850m** | 8192Mi -> **4096Mi** | 620m / 1100m | 3400Mi | 800m / 3900Mi | OVER | -88.98 USD |
| platform/otel-collector | DaemonSet | collector | 9 | 500m -> **200m** | 1024Mi -> **512Mi** | 140m / 180m | 410Mi | - | OVER | -58.21 USD |
| web/storefront | Deployment | nginx | 10 | 200m -> **50m** | 256Mi -> **64Mi** | 35m / 60m | 52Mi | 50m / 64Mi | OVER | -30.88 USD |
| web/storefront | Deployment | istio-proxy | 10 | 100m -> **100m** | 128Mi -> **128Mi** | 45m / 70m | 104Mi | - | OK | 0.00 USD |

## Notes

- `data/nightly-etl/etl`: Mixed change: one resource rises while the other falls; Batch workload: sized to p99; consider spot node pool + toleration
- `payments/ledger-worker/worker`: 3 OOMKills in window; memory raised to max+headroom; CPU throttled 42% of periods at p95; raise request/relax limit
- `platform/otel-collector/collector`: DaemonSet: replicas = node count; savings scale with cluster size
- `web/storefront/istio-proxy`: Within tolerance; no change

## How to apply

1. Start with UNDER-provisioned items: these are reliability fixes, not savings.
2. Apply OVER-provisioned changes per namespace via the generated Kustomize patch, one environment at a time.
3. Watch `container_cpu_cfs_throttled_periods_total` and OOM events for 7 days before the next round.
4. Re-run after HPA targets change: HPA scales on utilisation of *requests*, so lowering requests raises utilisation.
