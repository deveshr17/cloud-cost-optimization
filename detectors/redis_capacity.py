#!/usr/bin/env python3
"""Redis capacity planning from a series of INFO snapshots.

From daily ``used_memory``, ``keyspace_hits/misses``, ``evicted_keys`` and
ops/sec we derive:

* growth rate (GB/day via least-squares) and days until maxmemory is hit
* hit ratio and whether misses indicate an undersized or mis-TTL'd cache
* recommended provisioned size = projected 90-day peak * (1 + headroom),
  rounded to the provider's size steps, capped by replication overhead
* eviction policy recommendation based on TTL coverage and key patterns
* shard count for Memorystore Cluster / Redis Enterprise using per-shard
  memory and throughput ceilings

All numbers are examples on synthetic data; unit prices are placeholders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from detectors import common

GB = 1024**3
MEMORYSTORE_SIZE_STEPS_GB = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 160, 192, 256, 300]
SHARD_MAX_GB = 13.0  # practical per-shard ceiling for Memorystore Cluster / Redis Enterprise before rebalancing pain
SHARD_MAX_OPS = 25_000  # conservative single-threaded ops/sec ceiling per shard for mixed workloads


@dataclass
class CapacityPlan:
    instance: str
    provider: str
    tier: str
    provisioned_gb: float
    maxmemory_gb: float
    current_used_gb: float
    utilisation: float
    growth_gb_per_day: float
    days_to_maxmemory: float | None
    projected_90d_gb: float
    hit_ratio: float
    evictions_total: int
    ttl_coverage: float
    recommended_gb: int
    recommended_policy: str
    recommended_maxmemory_gb: float
    recommended_shards: int
    current_monthly_estimate: float
    recommended_monthly_estimate: float
    actions: list[str] = field(default_factory=list)


def _size_step(gb: float) -> int:
    for step in MEMORYSTORE_SIZE_STEPS_GB:
        if step >= gb:
            return step
    return int(math.ceil(gb / 50.0) * 50)


def analyze(info: dict[str, Any], pricing: dict[str, Any], *, headroom: float = 0.25, horizon_days: int = 90) -> CapacityPlan:
    samples = info["daily_samples"]
    used = [s["used_memory"] / GB for s in samples]
    dates = [common.parse_ts(s["date"]) for s in samples]
    span_days = max(1, (dates[-1] - dates[0]).days)
    slope_per_sample = common.linear_trend(used)
    growth_per_day = slope_per_sample * (len(used) - 1) / span_days if len(used) > 1 else 0.0

    current = used[-1]
    maxmem = info["maxmemory_bytes"] / GB
    provisioned = float(info["provisioned_memory_gb"])
    days_to_max = (maxmem - current) / growth_per_day if growth_per_day > 0 and current < maxmem else None
    projected = current + growth_per_day * horizon_days

    hits, misses = samples[-1]["keyspace_hits"], samples[-1]["keyspace_misses"]
    hit_ratio = hits / (hits + misses) if hits + misses else 1.0
    evictions = samples[-1]["evicted_keys"]
    ttl_cov = float(info.get("ttl_coverage_ratio", 0.0))

    actions: list[str] = []
    # Memory sizing: keep ~20% for replication buffers / fragmentation (Memorystore reserves via maxmemory-gb).
    needed = projected * (1 + headroom)
    recommended_gb = _size_step(needed)
    recommended_maxmem = round(recommended_gb * 0.8, 1)

    if days_to_max is not None and days_to_max < 60:
        actions.append(f"At current growth ({growth_per_day * 1024:.0f} MiB/day) maxmemory is reached in ~{days_to_max:.0f} days; resize or add eviction before then")
    if info.get("maxmemory_policy") == "noeviction" and ttl_cov < 0.9:
        actions.append(f"maxmemory-policy is noeviction while {1 - ttl_cov:.0%} of keys lack TTL: writes will fail at maxmemory. Switch to allkeys-lru or volatile-lru")
    if hit_ratio < 0.8:
        actions.append(f"Hit ratio {hit_ratio:.1%} is low for a cache; review TTLs and key design before adding memory")
    no_ttl_big = [k for k in info.get("big_keys", []) if not k.get("ttl_set")]
    for k in no_ttl_big:
        size_gb = k["count"] * k["avg_bytes"] / GB
        if size_gb > 0.05:
            actions.append(f"Key pattern `{k['key_pattern']}` holds {size_gb:.2f} GiB with no TTL; add TTL or move to a persistent store")
    if any(k["avg_bytes"] > 100 * 1024 * 1024 for k in info.get("big_keys", [])):
        actions.append("Big key detected (>100 MiB); a single sorted set blocks the event loop on range queries and cannot be sharded")

    # Eviction policy
    if ttl_cov >= 0.9:
        policy = "volatile-lru"
    elif info.get("tier", "").upper().startswith("STANDARD") or ttl_cov > 0.5:
        policy = "allkeys-lru"
    else:
        policy = "allkeys-lfu"

    # Sharding
    ops = float(info.get("ops_per_sec_p95", 0))
    shards_mem = math.ceil(needed / SHARD_MAX_GB)
    shards_ops = math.ceil(ops / SHARD_MAX_OPS) if ops else 1
    shards = max(1, shards_mem, shards_ops)
    if shards > 1:
        actions.append(f"Throughput/memory need {shards} shards ({shards_mem} by memory, {shards_ops} by ops); consider Memorystore for Redis Cluster or Redis Enterprise with hash-tagged keys")

    redis_p = pricing["redis"]
    rate_key = "memorystore_standard_gb_hour_m2" if info.get("tier", "").upper().startswith("STANDARD") else "memorystore_basic_gb_hour_m2"

    def tiered_rate(gb: float) -> float:
        if gb > 100:
            return redis_p["memorystore_standard_gb_hour_m5"] if "standard" in rate_key else redis_p[rate_key]
        if gb > 35:
            return redis_p["memorystore_standard_gb_hour_m4"] if "standard" in rate_key else redis_p[rate_key]
        return redis_p[rate_key]

    current_monthly = provisioned * tiered_rate(provisioned) * common.HOURS_PER_MONTH
    recommended_monthly = recommended_gb * tiered_rate(recommended_gb) * common.HOURS_PER_MONTH

    return CapacityPlan(
        instance=info["instance"],
        provider=info.get("provider", "memorystore"),
        tier=info.get("tier", ""),
        provisioned_gb=provisioned,
        maxmemory_gb=round(maxmem, 2),
        current_used_gb=round(current, 2),
        utilisation=round(current / maxmem, 3) if maxmem else 0.0,
        growth_gb_per_day=round(growth_per_day, 4),
        days_to_maxmemory=round(days_to_max, 1) if days_to_max is not None else None,
        projected_90d_gb=round(projected, 2),
        hit_ratio=round(hit_ratio, 4),
        evictions_total=evictions,
        ttl_coverage=ttl_cov,
        recommended_gb=recommended_gb,
        recommended_policy=policy,
        recommended_maxmemory_gb=recommended_maxmem,
        recommended_shards=shards,
        current_monthly_estimate=round(current_monthly, 2),
        recommended_monthly_estimate=round(recommended_monthly, 2),
        actions=actions,
    )


def render_markdown(info: dict[str, Any], plan: CapacityPlan, source: str) -> str:
    md = common.report_header(f"Redis capacity plan: {plan.instance}", source, common.fixed_now(info))
    md += "## Current state\n\n"
    md += common.md_table(
        ["Metric", "Value"],
        [
            ["Provider / tier", f"{plan.provider} / {plan.tier}"],
            ["Provisioned memory", f"{plan.provisioned_gb:.0f} GiB"],
            ["maxmemory", f"{plan.maxmemory_gb} GiB ({info.get('maxmemory_policy')})"],
            ["Used memory (latest)", f"{plan.current_used_gb} GiB ({plan.utilisation:.0%} of maxmemory)"],
            ["Growth", f"{plan.growth_gb_per_day * 1024:.0f} MiB/day"],
            ["Days until maxmemory", plan.days_to_maxmemory if plan.days_to_maxmemory is not None else "n/a"],
            ["Hit ratio", f"{plan.hit_ratio:.1%}"],
            ["Evicted keys (cumulative)", plan.evictions_total],
            ["TTL coverage", f"{plan.ttl_coverage:.0%}"],
            ["ops/sec p95", info.get("ops_per_sec_p95")],
            ["Connected clients p95", info.get("connected_clients_p95")],
        ],
    )
    md += "\n## Recommendation\n\n"
    md += common.md_table(
        ["Setting", "Current", "Recommended"],
        [
            ["Provisioned memory", f"{plan.provisioned_gb:.0f} GiB", f"{plan.recommended_gb} GiB (90d projection {plan.projected_90d_gb} GiB + headroom)"],
            ["maxmemory-gb", f"{plan.maxmemory_gb}", f"{plan.recommended_maxmemory_gb}"],
            ["maxmemory-policy", info.get("maxmemory_policy"), plan.recommended_policy],
            ["Shards", 1, plan.recommended_shards],
            ["Monthly estimate (placeholder prices)", common.fmt_money(plan.current_monthly_estimate), common.fmt_money(plan.recommended_monthly_estimate)],
        ],
    )
    md += "\n## Actions\n\n"
    for a in plan.actions or ["No action required."]:
        md += f"- {a}\n"
    md += "\n## Memory trend (GiB)\n\n"
    md += common.md_table(
        ["Date", "Used GiB", "Hit ratio"], [[s["date"], f"{s['used_memory'] / GB:.2f}", f"{s['keyspace_hits'] / (s['keyspace_hits'] + s['keyspace_misses']):.1%}"] for s in info["daily_samples"]]
    )
    return md


def collect_live(project: str, region: str, instance: str) -> dict[str, Any]:  # pragma: no cover
    redis_v1 = common.guarded_import("google.cloud.redis_v1")
    client = redis_v1.CloudRedisClient()
    inst = client.get_instance(name=f"projects/{project}/locations/{region}/instances/{instance}")
    common.LOG.warning("Live mode reads instance config only; daily_samples must come from Cloud Monitoring (redis.googleapis.com/stats/memory/usage).")
    return {
        "instance": inst.display_name or instance,
        "provider": "memorystore",
        "tier": redis_v1.Instance.Tier(inst.tier).name,
        "provisioned_memory_gb": inst.memory_size_gb,
        "maxmemory_bytes": int(inst.memory_size_gb * 0.8 * GB),
        "maxmemory_policy": inst.redis_configs.get("maxmemory-policy", "volatile-lru"),
        "daily_samples": [],
        "big_keys": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = common.base_parser("Plan Redis capacity from INFO snapshots.")
    parser.add_argument("--region", default="asia-south1")
    parser.add_argument("--instance", default="")
    parser.add_argument("--headroom", type=float, default=0.25)
    parser.add_argument("--horizon-days", type=int, default=90)
    args = parser.parse_args(argv)
    common.configure_logging(args.verbose)
    pricing = common.load_pricing(args.pricing)

    if common.require_offline_or_live(args):
        if not args.from_json:
            raise SystemExit("--dry-run requires --from-json.")
        info, source = common.load_json(args.from_json), args.from_json
    else:
        info, source = collect_live(args.project, args.region, args.instance), f"redis API {args.instance}"
        if not info["daily_samples"]:
            raise SystemExit("No samples collected; export Cloud Monitoring series to JSON and use --from-json.")

    plan = analyze(info, pricing, headroom=args.headroom, horizon_days=args.horizon_days)
    common.write_json(args.json_out, {"plan": plan})
    common.write_text(args.md_out, render_markdown(info, plan, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
