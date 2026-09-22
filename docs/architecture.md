# Architecture

The toolkit is four loosely coupled pipelines that share one findings model
(`detectors/common.py::Finding`) and one report format.

## 1. Billing analytics (BigQuery)

```text
Cloud Billing ──detailed export──▶ BigQuery billing_export ──▶ queries/*.sql ──▶ Looker Studio / scheduled query
                                                              └─▶ daily-anomaly-detection.sql ──▶ finops.anomalies ──▶ custom metric ──▶ alert policy
```

- Source of truth for spend. Everything else produces *estimates*; this produces the invoice.
- Queries always filter on `_PARTITIONTIME` and exclude the last two days from baselines because rows are restated.
- GKE cost allocation labels (`k8s-namespace`) make the export the showback layer for Kubernetes without a third-party agent.

## 2. Kubernetes rightsizing (Prometheus)

```text
kube-state-metrics + cAdvisor ──▶ Prometheus / Managed Prometheus ──▶ k8s_rightsizing.py ──▶ table + Kustomize patch
VPA (updateMode Off) ─────────────▶ kubectl get vpa -o json ────────▶ ──┘ (comparison column)
```

- 14-day window; p95 CPU and p99 memory working set per container, collapsed across pods by max.
- Recommendations are emitted as a strategic-merge patch that a platform team applies through the normal GitOps flow. The tool never writes to the cluster.

## 3. Inventory detectors (GCP APIs)

```text
Compute / SQL / GKE / GCS / Memorystore APIs ──▶ detectors/*.py ──▶ findings JSON + Markdown ──▶ GitHub issue / Slack / GCS
                                    fixtures/*.json ──▶ (offline mode, CI)
```

- Each detector has a `collect_live()` that shapes API responses into the same dict the fixtures use, so `analyze()` is tested once and used in both modes.
- Google client libraries are imported lazily; offline mode is standard-library only.

## 4. Cleanup automation (Cloud Scheduler -> Cloud Run Job)

```text
Cloud Scheduler ──OIDC──▶ Cloud Run Job (finops-job-runner SA, read-only) ──▶ reports ──▶ GCS + Slack
Human ──reviews report──▶ cleanup_orphaned_resources.sh --dry-run ──▶ --execute (separate SA with delete rights, logs to file)
```

The dry-run gate is structural, not procedural: the scheduled identity has no
delete permissions. Destruction requires a person running the script with a
different identity, after snapshots are taken.

## Data flow for one weekly cycle

1. Monday 07:00 IST: Scheduler triggers the job; detectors run live; reports land in `gs://…-finops-reports/YYYY-MM-DD/`.
2. GitHub Actions `scheduled-report.yml` (OIDC) runs the same detectors and opens an issue; last week's issue is closed with a link.
3. Team leads review the issue; items get `keep=true` labels or are queued for cleanup.
4. Cleanup runs in dry-run, output attached to the issue, then executed.
5. The following Monday's report shows the delta; the billing anomaly query confirms nothing unexpected happened.

## Security boundaries

| Component | Identity | Permissions |
| --- | --- | --- |
| Cloud Run Job | `finops-job-runner` | Viewer roles + object creator on reports bucket |
| Cloud Scheduler | `finops-scheduler-invoker` | `run.invoker` on one job |
| GitHub Actions | WIF-federated SA | Same viewer roles; no keys |
| Cleanup script | Engineer or break-glass SA | `compute.storageAdmin`, time-boxed |
| Looker Studio | `finops-looker` | Authorised view only |

## Extension points

- Add a detector: implement `analyze(data, pricing) -> list[Finding]`, a `render_markdown`, a fixture, and a test; register it in `Makefile` `report` and `main.py`.
- Add a cloud: the findings model is provider-agnostic; AWS Cost and Usage Reports load into BigQuery the same way and the anomaly query only needs column aliases.
