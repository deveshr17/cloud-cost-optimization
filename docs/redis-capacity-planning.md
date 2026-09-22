# Redis capacity planning

`detectors/redis_capacity.py` turns a series of `INFO` snapshots into a
sizing, eviction policy and shard recommendation. This page covers the
reasoning and what the tool cannot see.

## Inputs and what they tell you

| INFO field | Signal |
| --- | --- |
| `used_memory` over time | Growth rate; days until `maxmemory` |
| `used_memory_rss` vs `used_memory` | Fragmentation ratio; > 1.5 means the allocator is holding memory Redis is not using (`activedefrag yes`) |
| `keyspace_hits` / `keyspace_misses` | Hit ratio; a cache under 80-90% is either too small or holding the wrong keys |
| `evicted_keys` | Non-zero with `allkeys-*` policy means the working set exceeds `maxmemory`; with `volatile-*` it can mean nothing is expiring |
| `expired_keys` | TTL churn; healthy caches expire far more than they evict |
| `instantaneous_ops_per_sec`, `connected_clients` | Throughput and connection pressure; Redis is single-threaded for commands |
| `rejected_connections` | `maxclients` hit; usually a client pool leak |
| `latest_fork_usec` | Fork cost for RDB/AOF; grows with memory, argues for sharding past ~25 GB |

## Sizing

```text
needed   = projected_90d_used_memory x (1 + headroom)
provisioned = next provider size step >= needed
maxmemory   = ~80% of provisioned   (replication buffers, fragmentation, fork COW)
```

Headroom of 25% covers traffic growth between planning cycles. Memorystore
sets `maxmemory-gb` itself; keep it at or below 80% for Standard tier to
survive failover with a full replication buffer.

Growth is a least-squares slope over the samples; if the series is flat,
there is no deadline and the recommendation is driven by hit ratio and
eviction data alone.

## Eviction policy

| Situation | Policy |
| --- | --- |
| Every key has a TTL (coverage >= 90%) | `volatile-lru` — expiring keys go first, session-store semantics preserved |
| Pure cache, mixed TTLs | `allkeys-lru` — simplest correct choice |
| Hot-set is skewed (some keys read far more) | `allkeys-lfu` — protects the popular 5% from a scan of the cold 95% |
| Redis is the primary store | `noeviction` **and** an alert well before `maxmemory`; writes fail at the limit |

`noeviction` on a cache with untracked growth is the most common Redis
outage pattern: `OOM command not allowed` at 03:00. The tool flags it.

## Sharding

Shard when either ceiling is close:

- **Memory**: > ~13 GB per shard makes fork-based persistence and failover slow.
- **Throughput**: > ~25k ops/s of mixed commands per shard on a single core; simple GETs go higher, `ZRANGE`/`SUNION` much lower.

Options: Memorystore for Redis Cluster (OSS cluster protocol, client must
support it), Redis Enterprise (proxy hides sharding), or application-level
sharding by key hash. Use hash tags (`{user:123}:profile`) so multi-key
operations stay on one shard.

## Big keys and key design

A single 380 MB sorted set cannot be sharded and blocks the event loop on
range queries. Find them with `redis-cli --bigkeys` (sampling) or
`MEMORY USAGE`. Fixes: split by time bucket, cap with `ZREMRANGEBYRANK`,
or move leaderboards to a purpose-built store.

Keys without TTL in a cache are a slow leak. Rate-limit counters must use
`EXPIRE` or `SET ... EX`; audit with `SCAN` + `TTL` sampling.

## Cost model

Memorystore bills per provisioned GB-hour with tiered rates (M1 to M5) as
size grows; Standard tier is roughly 1.7x Basic for the replica. Doubling
capacity to survive a growth curve is often cheaper than one incident, but
the report shows the increase honestly so the team can weigh fixing TTLs
first.
