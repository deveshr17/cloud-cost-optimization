# Idle resource report: example-project-dev

Generated: 2026-09-21 02:00 UTC  
Source: `detectors/fixtures/idle_resources.json`

> All cost figures are **estimates computed from placeholder unit prices on synthetic data**. They illustrate the calculation method and are not realised savings.

## Summary

| Metric | Value |
| --- | --- |
| Findings (actionable) | 10 |
| Findings excluded by `keep=true` | 4 |
| Estimated monthly run-rate of actionable findings | 934.44 USD |

### By category

| Category | Count | Est. monthly |
| --- | --- | --- |
| empty-node-pool | 1 | 432.16 USD |
| unused-cloudsql | 1 | 277.30 USD |
| unattached-disk | 2 | 150.00 USD |
| old-snapshot | 3 | 32.32 USD |
| lb-no-backends | 1 | 21.90 USD |
| stopped-vm-with-disks | 1 | 12.00 USD |
| idle-static-ip | 1 | 8.76 USD |

## Actionable findings

| Category | Resource | Severity | Est. monthly | Recommendation |
| --- | --- | --- | --- | --- |
| empty-node-pool | `gke-dev-primary/nodePools/ml-experiments` | high | 432.16 USD | No workload pods for 14d; enable autoscaling with min 0 or delete the pool |
| unused-cloudsql | `asia-south1/sql/mysql-legacy-cms` | high | 277.30 USD | p95 connections=0 over 14d; export, stop (activation policy NEVER) for 30d, then delete |
| unattached-disk | `asia-south1-a/disks/pd-orphan-postgres-data` | high | 102.00 USD | Snapshot then delete (unattached 141d). Snapshot cost ~16.00 USD/mo |
| unattached-disk | `asia-south1-a/disks/pd-legacy-jenkins-home` | medium | 48.00 USD | Snapshot then delete (unattached 221d). Snapshot cost ~32.00 USD/mo |
| lb-no-backends | `asia-south1/forwardingRules/fr-legacy-admin` | medium | 21.90 USD | Forwarding rule has zero backends; delete rule, target and any reserved IP |
| old-snapshot | `snapshots/snap-jenkins-2024-12-24` | medium | 19.20 USD | 636d old (600.0 GiB); delete or move to a snapshot schedule with retention |
| stopped-vm-with-disks | `asia-south1-c/instances/batch-worker-legacy` | medium | 12.00 USD | Stopped 155d; create machine image and delete, or confirm restart plan |
| idle-static-ip | `asia-south1/addresses/ip-old-lb-frontend` | low | 8.76 USD | Release reserved IP 203.0.113.10 (unused 294d) |
| old-snapshot | `snapshots/snap-postgres-2025-11-01` | low | 6.72 USD | 324d old (210.0 GiB); delete or move to a snapshot schedule with retention |
| old-snapshot | `snapshots/snap-postgres-2025-10-01` | low | 6.40 USD | 355d old (200.0 GiB); delete or move to a snapshot schedule with retention |

## Excluded (label `keep=true`)

| Category | Resource | Severity | Est. monthly | Recommendation |
| --- | --- | --- | --- | --- |
| unattached-disk | `asia-south1-b/disks/pd-ci-runner-scratch` | medium | 24.00 USD | Excluded by label keep=true; review owner |
| unused-cloudsql | `asia-south1/sql/pg-analytics-replica` | low | 554.60 USD | Excluded by label keep=true |
| idle-static-ip | `asia-south1/addresses/ip-nat-standby` | low | 8.76 USD | Excluded by label keep=true |
| old-snapshot | `snapshots/snap-compliance-hold-2025` | low | 3.20 USD | Excluded by label keep=true (retention hold) |

## Next steps

1. Review high-severity items with resource owners (label `team`).
2. Run `cleanup/cleanup_orphaned_resources.sh` in dry-run mode to confirm the candidate list matches.
3. Execute cleanup with `--execute` only after snapshots/exports are verified.
