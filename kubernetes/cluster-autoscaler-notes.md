# GKE autoscaling and cost controls

Notes on the cluster-level levers that determine how much of the requested
capacity you actually pay for. Terraform for all of these lives in
`terraform/gke-cost-controls/`.

## 1. Autoscaling profile: `optimize-utilization`

```bash
gcloud container clusters update gke-dev-primary --region asia-south1 \
  --autoscaling-profile optimize-utilization
```

| Profile | Behaviour | Use when |
| --- | --- | --- |
| `balanced` (default) | Conservative scale-down (10 min unneeded window), keeps headroom | Prod with strict latency SLOs |
| `optimize-utilization` | Scales down aggressively, prefers packing pods onto fewer nodes, lower scale-down delay | Dev/UAT, batch, and prod clusters with PDBs and good readiness probes |

Trade-off: more frequent pod evictions during scale-down. It is safe only
when every workload has a `PodDisruptionBudget` and readiness gating;
otherwise a scale-down can take a service to zero ready replicas.

## 2. Node auto-provisioning (NAP)

NAP creates node pools on demand sized to pending pods, then deletes them
when empty. It removes the "one oversized pool for the one big job"
anti-pattern.

```hcl
cluster_autoscaling {
  enabled             = true
  autoscaling_profile = "OPTIMIZE_UTILIZATION"
  resource_limits { resource_type = "cpu"    minimum = 4  maximum = 256 }
  resource_limits { resource_type = "memory" minimum = 16 maximum = 1024 }
  auto_provisioning_defaults {
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    management { auto_repair = true auto_upgrade = true }
    shielded_instance_config { enable_secure_boot = true }
  }
}
```

Set `resource_limits` deliberately: they are the hard ceiling on what NAP
can provision and are your cost circuit-breaker.

## 3. Spot node pools for batch

Spot VMs are 60-91% cheaper than on-demand but can be preempted with 30s
notice. Confine them to work that tolerates restarts.

Pool (Terraform: `google_container_node_pool.batch_spot`):

```hcl
node_config {
  spot = true
  taint {
    key    = "cloud.google.com/gke-spot"
    value  = "true"
    effect = "NO_SCHEDULE"
  }
  labels = { workload-class = "batch" }
}
autoscaling { min_node_count = 0 max_node_count = 20 }
```

Workload:

```yaml
spec:
  tolerations:
    - key: cloud.google.com/gke-spot
      operator: Equal
      value: "true"
      effect: NoSchedule
  nodeSelector:
    cloud.google.com/gke-spot: "true"
  terminationGracePeriodSeconds: 25   # must finish inside the 30s preemption notice
```

Checklist before moving a workload to spot:

- Idempotent or checkpointed (Jobs with `backoffLimit`, queue consumers with ack-after-commit).
- No single-replica stateful component.
- Prefer several machine families in the pool (`n2d`, `n2`, `e2`) via multiple pools or NAP so one family's capacity crunch does not stall the queue.
- `PodDisruptionBudget` does not protect against preemption; do not rely on it.

## 4. PDB implications

Cluster autoscaler respects PDBs when draining. Two common misconfigurations
block scale-down indefinitely and inflate the bill:

| Misconfiguration | Effect | Fix |
| --- | --- | --- |
| `minAvailable: 100%` or `maxUnavailable: 0` | Node never drains | Use `maxUnavailable: 1` (or 25% for large replica sets) |
| PDB on a single-replica Deployment | Node with that pod never drains | Run 2 replicas, or accept the disruption by removing the PDB in dev |
| `kube-system` pods without PDBs (some add-ons) | Autoscaler refuses to evict | Add PDBs or the `cluster-autoscaler.kubernetes.io/safe-to-evict: "true"` annotation |

Find blockers:

```bash
kubectl get events -A --field-selector reason=ScaleDown | grep -i "cannot be removed"
kubectl get pdb -A -o json | jq -r '.items[] | select(.status.disruptionsAllowed==0) | "\(.metadata.namespace)/\(.metadata.name)"'
```

## 5. Other scale-down blockers

- Pods with local `emptyDir` storage and no `safe-to-evict` annotation.
- Pods not managed by a controller (bare pods).
- Nodes with the `cluster-autoscaler.kubernetes.io/scale-down-disabled: "true"` annotation.
- Pods with `nodeAffinity` that only matches the current node.

## 6. Bin packing

Autoscaler removes a node when its requested utilisation is below
`--scale-down-utilization-threshold` (0.5 default; `optimize-utilization`
raises effective packing). Nodes hover above the threshold when requests
are inflated, so rightsizing requests (see `detectors/k8s_rightsizing.py`)
is what actually lets nodes drain. The relevant Grafana panel is
"Requested but unused CPU by namespace" in `dashboards/grafana-cost-dashboard.json`.

## 7. Nonprod schedules

Two complementary approaches, both in this repo:

1. **Workload level**: `kube-downscaler-values.yaml` scales Deployments to 0 replicas outside `Mon-Fri 08:00-20:00 Asia/Kolkata`; autoscaler then removes empty nodes.
2. **Node pool level**: `cleanup/gke-scale-down-nonprod.sh` resizes pools to 0 directly; faster and works for clusters without the autoscaler, but pods go Pending until morning.

Use (1) by default; (2) for clusters where you want a hard guarantee that
nothing runs at night.
