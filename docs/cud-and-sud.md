# Committed use and sustained use discounts

## Sustained use discounts (SUD)

Automatic, no commitment. For N1/N2/N2D/C2/M1/M2 machine families running
more than 25% of the month, the price steps down to a maximum of ~30% off
at 100% utilisation. E2, T2D, A2 and spot VMs do **not** get SUD.

Implications:
- SUD applies per vCPU/memory in a region across instances, so restarting or replacing VMs does not lose it.
- Moving N2 to E2 loses SUD but E2's list price is lower; compare effective prices, not list.
- CUD-covered usage does not also receive SUD; buying CUDs on a fleet that already gets 30% SUD yields only the difference.

## Committed use discounts (CUD)

| Type | Covers | Term | Discount | Flexibility |
| --- | --- | --- | --- | --- |
| Resource-based | vCPU + memory of a machine family in a region (also GPUs, local SSD, SLES/RHEL) | 1 or 3 years | ~37% / ~55% (varies by family) | Fixed family and region; usable by any project under the billing account |
| Spend-based (flexible CUD) | Hourly spend across eligible services (Compute Engine general purpose, Cloud SQL, GKE Autopilot, Cloud Run, Spanner, etc.) | 1 or 3 years | ~28% / ~46% | Family- and region-agnostic |

Billing appears as a `Commitment v1: ...` fee line plus negative
`COMMITTED_USAGE_DISCOUNT` credits on the matching usage.
`billing/queries/committed-use-coverage.sql` reports both.

## How much to buy

1. Finish waste removal, scheduling and rightsizing first. A commitment on inflated requests locks the waste in for a year.
2. Plot hourly on-demand eligible usage for 90 days (vCPU-hours by family and region).
3. Commit to the **trough**, not the average: the level of usage present in the 95th percentile of hours (i.e. almost always running). Cover the rest with SUD or spot.
4. Prefer flexible CUDs for the first tranche when the fleet is still changing (migrations, family changes); add resource-based CUDs for stable, large families where the deeper discount pays.
5. Stagger terms so no more than a third expires in one quarter.

## Metrics to watch

| Metric | Healthy | Query |
| --- | --- | --- |
| Coverage (% of eligible on-demand cost with a CUD credit) | 60-80% | `committed-use-coverage.sql` |
| Utilisation (% of commitment fee matched by credits) | > 95% | `idle_cud_cost` column |
| Idle commitment | ~0 | same |

Coverage above 90% is usually a sign of under-buying flexibility rather than
excellence: any downturn strands commitment.

## Other discount programs

- **Spot VMs**: 60-91% off, preemptible; batch and stateless only (`cluster-autoscaler-notes.md`).
- **BigQuery editions commitments**: slot capacity 1y/3y; evaluated by `bigquery_optimizer.py`.
- **Cloud SQL / Memorystore / Spanner**: spend-based CUDs; same trough logic.
- **Negotiated discounts** appear as `DISCOUNT` / `SPENDING_BASED_DISCOUNT` credits; check they land on the expected SKUs each month with `credits-and-discounts.sql`.

## Reviewing commitments

Quarterly: pull the coverage query, compare against the forecast from
product roadmaps, and record the decision in the FinOps review. Commitments
are a finance decision informed by engineering data; the platform team's job
is to make the data accurate.
