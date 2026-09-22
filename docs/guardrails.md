# Guardrails

Detection finds waste after it exists. Guardrails stop the most expensive
mistakes before the bill arrives. Ordered from cheapest to enforce.

## Budgets and alerts

- One `google_billing_budget` per project (Terraform: `terraform/gke-cost-controls/main.tf`), thresholds at 50/80/90/100% of actual and 100/120% of **forecast**. The forecast rule is the useful one: it fires mid-month when the run-rate is wrong.
- Notifications go to Pub/Sub (Slack notifier, automation) and a Monitoring channel; `disable_default_iam_recipients = true` so billing admins are not the only people who hear about it.
- A daily spend anomaly alert on a custom metric fed by `daily-anomaly-detection.sql`.
- Budgets do **not** stop spend. Automated caps (disable billing via a Cloud Function on the Pub/Sub topic) are appropriate only for sandbox projects.

## Quotas

| Quota | Why |
| --- | --- |
| Compute: CPUs per region, per project | Caps a runaway autoscaler or a fat-fingered node count |
| Compute: persistent disk total GB | Stops disk sprawl from CI jobs |
| BigQuery: query usage per day (project and per user) | Analyst circuit breaker |
| GKE NAP `resource_limits` | Hard ceiling on auto-provisioned capacity |
| Kubernetes `ResourceQuota` per namespace | Team-level cap on requests (`kubernetes/resource-quotas.yaml`) |
| Cloud Run max instances | Bounds a hot loop calling your own API |

Request quota increases deliberately, per environment; the default limits
in a fresh project are a feature.

## Organization policies

| Constraint | Effect |
| --- | --- |
| `compute.vmExternalIpAccess` | No public IPs on VMs; egress goes through NAT, which is metered and visible |
| `compute.restrictCloudNATUsage` / `compute.skipDefaultNetworkCreation` | No accidental default networks with open egress |
| `gcp.resourceLocations` | Pins regions; prevents accidental multi-region storage or cross-region egress |
| `compute.requireOsLogin`, `iam.disableServiceAccountKeyCreation` | Security, but also stops leaked keys becoming crypto-mining bills |
| `compute.vmCanIpForward`, `compute.restrictLoadBalancerCreationForTypes` | Blocks classic LB types nobody should be paying for |
| Custom constraint on `container.googleapis.com/Cluster` requiring `costManagementConfig.enabled` | Every cluster produces namespace-level billing labels |

## Labels and tagging enforcement

- Terraform modules take `labels` as a required variable with `env`, `team`, `cost-center`, `app` keys validated.
- CI: `tflint` / OPA policy rejects resources without the standard labels.
- Kubernetes: admission policy (Gatekeeper/Kyverno) requiring `team` and `cost-center` labels on namespaces; `LimitRange` defaults so no pod is BestEffort.
- Weekly report lists `(unlabelled)` spend by project; owners fix labels or the spend is charged to the platform team's cost center, which motivates fixing labels.

## Automation safety

- Detectors run as read-only identities. Deletion requires a human with a different identity and `--execute`.
- `keep=true` label is honoured by every cleanup path.
- Disks are snapshotted before deletion; snapshots are labelled `source=cleanup` and excluded from the snapshot cleanup for 30 days.
- Nonprod downscaling refuses any project or cluster whose name contains `prod`.
- All cleanup actions append to a log file and the weekly issue.

## Access

- Billing export dataset: viewer role to a group, authorised views for dashboards, no individual grants.
- Billing account roles (`billing.costsManager`, `billing.admin`) restricted to the FinOps group; `billing.viewer` for team leads.
- Break-glass for cleanup: a service account impersonation with a 1-hour token lifetime, audited in Cloud Audit Logs.
