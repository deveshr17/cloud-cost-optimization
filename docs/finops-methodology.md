# FinOps methodology

How this toolkit maps onto the FinOps Foundation's Inform / Optimize / Operate
loop, and the conventions that make the numbers trustworthy.

## Inform

Goal: every unit of spend is attributable to a team and a purpose within 24 hours.

| Capability | Implementation |
| --- | --- |
| Allocation | Billing export + `cost-by-project-label-env.sql`; GKE cost allocation for namespace-level showback |
| Unlabelled spend | Reported explicitly as `(unlabelled)`; target is < 2% of monthly spend |
| Dashboards | Looker Studio for finance/leads, Grafana for engineers (requested-vs-used), Cloud Monitoring for on-call |
| Anomalies | z-score query daily; alert policy on the derived metric |
| Unit economics | Cost per order / per 1k requests: join `net_cost` by `app` label to a business metric table. Trend matters more than the absolute value |

### Tagging standard

Applied as project labels (inherited into billing rows) **and** resource
labels (needed for shared projects). Enforced in Terraform modules and by a
CI check that rejects resources without them.

| Label | Values | Purpose |
| --- | --- | --- |
| `env` | `dev`, `uat`, `prod` | Environment split; drives nonprod schedules and cleanup thresholds |
| `team` | lowercase team slug | Showback owner; who receives the report |
| `cost-center` | `cc-NNNN` | Finance mapping |
| `app` | lowercase app slug | Unit economics and service-level views |
| `keep` (optional) | `true` | Exclusion from automated cleanup |

Rules: lowercase, hyphens, no PII, values from an allow-list where possible.
Kubernetes namespaces carry the same keys as namespace labels so the GKE
namespace-to-team mapping is a simple join.

## Optimize

Ordered by effort-to-value; do them in this order.

1. **Waste removal** (`idle_resources.py`, `cleanup/`): orphaned disks, snapshots, IPs, empty pools, idle databases. Zero risk to running workloads.
2. **Scheduling**: nonprod off at night and weekends (`kube-downscaler`, `gke-scale-down-nonprod.sh`). Roughly 65% of hours in a week are outside 08:00-20:00 Mon-Fri.
3. **Rightsizing** (`k8s_rightsizing.py`, VPA): requests to p95/p99 plus headroom. Releases node capacity the autoscaler can then remove.
4. **Architecture** (`storage_optimizer.py`, `bigquery_optimizer.py`, `redis_capacity.py`): lifecycle classes, partitioning, eviction policies. Larger changes, owner involvement required.
5. **Rates** (`cud-and-sud.md`): commit only after 1-4 have stabilised the baseline; committing to waste locks it in.

Every recommendation carries an estimate computed from `pricing.example.json`.
Estimates are for prioritisation; realised savings are read from the billing
export a month later, never from the tool.

## Operate

- **Cadence**: weekly automated report (issue), monthly review with team leads, quarterly commitment review.
- **Ownership**: the platform team runs the tooling; the team named in the `team` label owns the decision on each finding.
- **Guardrails** (`guardrails.md`): budgets with forecast alerts, quotas, org policies that stop the most expensive mistakes before they happen.
- **KPIs**: unlabelled %, idle-resource run-rate, requested-vs-used ratio per namespace, CUD coverage and utilisation, cost per business unit.
- **Feedback loop**: every executed cleanup is logged; every rightsizing patch goes through the same PR flow as application changes; regressions show up in the next anomaly run.

## What this toolkit deliberately does not do

- Modify resources automatically. Dry-run is the default everywhere and the scheduled identity is read-only.
- Claim savings. It reports estimates and the method; the bill is the only source of truth.
- Replace a commercial FinOps platform for multi-cloud chargeback. It covers the 80% that a platform team can own directly.
