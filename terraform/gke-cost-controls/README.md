# terraform/gke-cost-controls

Cost-control baseline for a regional GKE cluster:

| Control | Resource | Effect |
| --- | --- | --- |
| Cost allocation | `cost_management_config` | `k8s-namespace` labels in billing export for showback |
| Optimize-utilization profile + NAP | `cluster_autoscaling` | Aggressive scale-down, right-shaped node pools, hard vCPU/memory ceilings |
| Spot batch pool | `google_container_node_pool.batch_spot` | 0-N spot nodes with `gke-spot` taint |
| VPA (recommend mode) | `vertical_pod_autoscaling` | Feeds `detectors/k8s_rightsizing.py --vpa-json` |
| Budget | `google_billing_budget` | 50/80/90/100% actual + 100/120% forecast, Pub/Sub + email |
| Anomaly alert | `google_monitoring_alert_policy` | Daily spend above threshold on a custom metric |

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars   # edit placeholders
terraform init                                 # uses the GCS backend in backend.tf
terraform plan
terraform apply
```

Validate without credentials or a backend:

```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

## Requirements

- Terraform >= 1.6, `hashicorp/google` and `google-beta` ~> 6.0
- Roles on the deploying identity: `roles/container.admin`, `roles/iam.serviceAccountAdmin`,
  `roles/resourcemanager.projectIamAdmin` (scoped), `roles/billing.costsManager` on the billing account,
  `roles/monitoring.editor`, `roles/pubsub.editor`.
- An existing VPC with `pods` and `services` secondary ranges.

## Inputs and outputs

See `variables.tf` (every variable has a description; `env` and `billing_account_id` are validated)
and `outputs.tf`.
