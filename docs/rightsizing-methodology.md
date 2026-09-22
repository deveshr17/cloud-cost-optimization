# Rightsizing methodology

`detectors/k8s_rightsizing.py` implements the rules below. This document
explains why they are what they are.

## Data

- **Window**: 14 days. Long enough to include two weekly cycles and a deploy or two; short enough to react. Batch workloads with monthly peaks need 35 days.
- **CPU**: `rate(container_cpu_usage_seconds_total[5m])` sampled every 5 minutes; percentiles across the window per container, then max across pods of the same workload.
- **Memory**: `container_memory_working_set_bytes` (what the OOM killer looks at), not RSS or cache.
- **Reliability signals**: `container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total` and OOMKilled terminations. These veto downward changes.

## Rules

| Resource | Request | Limit | Rationale |
| --- | --- | --- | --- |
| CPU | p95 x 1.30, rounded up to 50m | none by default (`--cpu-limits 2x` to emit) | CPU is compressible; over-request wastes nodes, under-request only slows |
| Memory | p99 x 1.20, rounded up to 32Mi | = request (Guaranteed) | Memory is incompressible; exceeding the limit kills the pod |

Changes under 15% in both dimensions are suppressed to avoid churn.

### Why the CPU/memory asymmetry

CPU contention degrades gracefully: the scheduler shares time and the app
gets slower. Memory contention does not: the kernel kills the process. So
CPU gets a tighter percentile and generous scale-out via HPA, while memory
gets the tail percentile and a limit equal to the request so the pod is
never scheduled somewhere it cannot actually fit.

### Under-provisioned containers first

If OOM kills > 0 or throttling > 25% of periods at p95, the container is
sized from the max/p99 and flagged `UNDER`. These are reliability fixes and
are applied before any savings work; otherwise "rightsizing" is remembered as
the project that broke things.

## The CPU limits question

Two defensible positions; the tool supports both.

**No CPU limits** (default here)
- Requests still guarantee a fair share under contention; limits only cap the burst above it.
- CFS throttling with limits causes latency spikes at p99 even when average usage is low, because bursts are quantised into 100ms periods.
- Bin packing is driven by requests, so removing limits does not change scheduling.

**With CPU limits**
- Predictable behaviour: a runaway process cannot starve neighbours beyond its request share. Matters on nodes with noisy multi-tenant workloads.
- Required for the Guaranteed QoS class (limits == requests), which affects eviction order and, on GKE Standard, static CPU manager pinning.
- Some compliance baselines require limits on every container.

Recommendation: no CPU limits for latency-sensitive services on dedicated
node pools; limits at 2x request for shared dev clusters and for anything
that has previously caused a noisy-neighbour incident. Memory limits always.

## HPA interplay

HPA scales on *utilisation of requests*. Lowering a request from 1000m to
300m turns 60% utilisation into 200% and the HPA scales out immediately. So:

1. Lower requests and retune the HPA target in the same change, or
2. Switch the HPA to an absolute metric (RPS per pod, queue depth) which is unaffected by requests.

Never rightsize a workload with an HPA without looking at the HPA.

## VPA comparison

VPA in `updateMode: Off` produces a target from a decaying histogram (default
half-life 24h, so recent days weigh more). The tool compares its p95-based
number and flags divergence over 50%. Large divergence usually means a
recent traffic change; look before applying either.

## Rollout

1. Apply the patch to dev, watch throttling/OOM panels for 3 days.
2. UAT for 7 days including a load test.
3. Prod per namespace, largest saving first, one namespace per day.
4. Re-run the detector after 14 days; expect a second, smaller round.

## Where the savings actually come from

Requests shrink; pods do not get cheaper. Savings materialise only when the
cluster autoscaler removes nodes. Verify with `kube_node_info` count and the
billing export, and check `cluster-autoscaler-notes.md` for scale-down
blockers (PDBs, local storage, un-evictable pods) when nodes do not go away.
