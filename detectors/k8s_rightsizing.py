#!/usr/bin/env python3
"""Right-size Kubernetes container requests/limits from 14 days of Prometheus data.

Method (see docs/rightsizing-methodology.md)
--------------------------------------------
* CPU request      = p95(usage) * (1 + cpu_headroom)            rounded up to 50m
* CPU limit        = optional; when --cpu-limits is 'none' no limit is emitted,
                     otherwise max(p99 * 2, request * 2)
* Memory request   = p99(working_set) * (1 + mem_headroom)      rounded up to 32Mi
* Memory limit     = request (Guaranteed for memory; avoids OOM under node pressure)
* If OOM kills > 0 or throttling ratio > 0.25 the container is flagged as
  UNDER-provisioned and bumped by an extra safety factor.

Data sources
------------
--from-json  : fixture shaped like ``collect_from_prometheus`` output
default      : Prometheus HTTP API (``PROMETHEUS_URL``), stdlib urllib only
--vpa-json   : optional flattened VPA recommendations for side-by-side comparison
--patch-out  : write a Kustomize strategic-merge patch YAML with the new values
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from detectors import common
from detectors.common import HOURS_PER_MONTH

PROM_QUERIES = {
    "cpu_p95_m": 'quantile_over_time(0.95, sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{container!="", container!="POD"}}[5m]))[{lookback}:5m]) * 1000',
    "cpu_p99_m": 'quantile_over_time(0.99, sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{container!="", container!="POD"}}[5m]))[{lookback}:5m]) * 1000',
    "cpu_max_m": 'max_over_time(sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{container!="", container!="POD"}}[5m]))[{lookback}:5m]) * 1000',
    "mem_p99_mi": 'quantile_over_time(0.99, container_memory_working_set_bytes{{container!="", container!="POD"}}[{lookback}:5m]) / 1024 / 1024',
    "mem_max_mi": 'max_over_time(container_memory_working_set_bytes{{container!="", container!="POD"}}[{lookback}:5m]) / 1024 / 1024',
    "cpu_request_m": 'kube_pod_container_resource_requests{{resource="cpu"}} * 1000',
    "mem_request_mi": 'kube_pod_container_resource_requests{{resource="memory"}} / 1024 / 1024',
    "throttled_ratio_p95": "quantile_over_time(0.95, (rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m]))[{lookback}:5m])",
    "oom_kills_14d": "increase(container_oom_events_total[{lookback}])",
}


@dataclass
class Recommendation:
    namespace: str
    workload: str
    kind: str
    container: str
    replicas: int
    current_cpu_request_m: int
    current_mem_request_mi: int
    cpu_p95_m: float
    cpu_p99_m: float
    mem_p99_mi: float
    rec_cpu_request_m: int
    rec_cpu_limit_m: int | None
    rec_mem_request_mi: int
    rec_mem_limit_mi: int
    status: str  # over | under | ok
    monthly_delta_estimate: float  # negative = savings
    notes: list[str] = field(default_factory=list)
    vpa_target_cpu_m: int | None = None
    vpa_target_mem_mi: int | None = None

    @property
    def cpu_delta_cores(self) -> float:
        return (self.rec_cpu_request_m - self.current_cpu_request_m) * self.replicas / 1000.0

    @property
    def mem_delta_gib(self) -> float:
        return (self.rec_mem_request_mi - self.current_mem_request_mi) * self.replicas / 1024.0


def recommend(
    c: dict[str, Any],
    pricing: dict[str, Any],
    *,
    cpu_headroom: float = 0.30,
    mem_headroom: float = 0.20,
    cpu_limits: str = "none",
    min_change_pct: float = 0.15,
) -> Recommendation:
    notes: list[str] = []
    under = c.get("oom_kills_14d", 0) > 0 or c.get("throttled_ratio_p95", 0.0) > 0.25
    if c.get("oom_kills_14d", 0) > 0:
        notes.append(f"{c['oom_kills_14d']} OOMKills in window; memory raised to max+headroom")
    if c.get("throttled_ratio_p95", 0.0) > 0.25:
        notes.append(f"CPU throttled {c['throttled_ratio_p95']:.0%} of periods at p95; raise request/relax limit")

    cpu_basis = c["cpu_p95_m"] if not under else max(c["cpu_p99_m"], c["cpu_p95_m"])
    mem_basis = c["mem_p99_mi"] if not under else max(c.get("mem_max_mi", c["mem_p99_mi"]), c["mem_p99_mi"])

    rec_cpu = common.round_up_cpu_millicores(cpu_basis * (1 + cpu_headroom))
    rec_mem = common.round_up_mebibytes(mem_basis * (1 + mem_headroom))

    if cpu_limits == "none":
        rec_cpu_limit: int | None = None
    else:
        rec_cpu_limit = common.round_up_cpu_millicores(max(c["cpu_p99_m"] * 2, rec_cpu * 2))
    rec_mem_limit = rec_mem  # memory Guaranteed

    cur_cpu, cur_mem = int(c["cpu_request_m"]), int(c["mem_request_mi"])
    cpu_change = (rec_cpu - cur_cpu) / cur_cpu if cur_cpu else 1.0
    mem_change = (rec_mem - cur_mem) / cur_mem if cur_mem else 1.0

    k8s = pricing["kubernetes"]

    def monthly(cpu_m: int, mem_mi: int) -> float:
        return (cpu_m / 1000.0 * k8s["vcpu_hour"] + mem_mi / 1024.0 * k8s["gb_hour"]) * c["replicas"] * HOURS_PER_MONTH

    if under:
        status = "under"
    elif abs(cpu_change) < min_change_pct and abs(mem_change) < min_change_pct:
        status, rec_cpu, rec_mem, rec_mem_limit = "ok", cur_cpu, cur_mem, int(c.get("mem_limit_mi", cur_mem))
        notes.append("Within tolerance; no change")
    else:
        # Net cost direction decides the label; mixed changes (CPU up, memory down) are common for batch jobs.
        status = "over" if monthly(rec_cpu, rec_mem) < monthly(cur_cpu, cur_mem) else "under"
        if (rec_cpu > cur_cpu) != (rec_mem > cur_mem):
            notes.append("Mixed change: one resource rises while the other falls")

    if c.get("kind") == "CronJob":
        notes.append("Batch workload: sized to p99; consider spot node pool + toleration")
    if c.get("kind") == "DaemonSet":
        notes.append("DaemonSet: replicas = node count; savings scale with cluster size")

    monthly_delta = monthly(rec_cpu, rec_mem) - monthly(cur_cpu, cur_mem)

    return Recommendation(
        namespace=c["namespace"],
        workload=c["workload"],
        kind=c.get("kind", "Deployment"),
        container=c["container"],
        replicas=int(c["replicas"]),
        current_cpu_request_m=cur_cpu,
        current_mem_request_mi=cur_mem,
        cpu_p95_m=c["cpu_p95_m"],
        cpu_p99_m=c["cpu_p99_m"],
        mem_p99_mi=c["mem_p99_mi"],
        rec_cpu_request_m=rec_cpu,
        rec_cpu_limit_m=rec_cpu_limit,
        rec_mem_request_mi=rec_mem,
        rec_mem_limit_mi=rec_mem_limit,
        status=status,
        monthly_delta_estimate=round(monthly_delta, 2),
        notes=notes,
    )


def attach_vpa(recs: list[Recommendation], vpa: dict[str, Any] | None) -> None:
    if not vpa:
        return
    index = {(r["namespace"], r["workload"], r["container"]): r for r in vpa.get("recommendations", [])}
    for rec in recs:
        hit = index.get((rec.namespace, rec.workload, rec.container))
        if hit:
            rec.vpa_target_cpu_m = int(hit["target_cpu_m"])
            rec.vpa_target_mem_mi = int(hit["target_mem_mi"])
            cpu_gap = abs(rec.rec_cpu_request_m - rec.vpa_target_cpu_m) / max(rec.vpa_target_cpu_m, 1)
            if cpu_gap > 0.5:
                rec.notes.append(f"Diverges from VPA target by {cpu_gap:.0%} on CPU; inspect before applying")


def analyze(data: dict[str, Any], pricing: dict[str, Any], vpa: dict[str, Any] | None = None, **kw: Any) -> list[Recommendation]:
    recs = [recommend(c, pricing, **kw) for c in data["containers"]]
    attach_vpa(recs, vpa)
    order = {"under": 0, "over": 1, "ok": 2}
    return sorted(recs, key=lambda r: (order[r.status], r.monthly_delta_estimate))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def render_markdown(data: dict[str, Any], recs: list[Recommendation], source: str, cpu_limits: str) -> str:
    md = common.report_header(f"Rightsizing report: {data.get('cluster', 'cluster')} ({data.get('lookback', '14d')} lookback)", source, common.fixed_now(data))
    savings = sum(r.monthly_delta_estimate for r in recs if r.monthly_delta_estimate < 0)
    increases = sum(r.monthly_delta_estimate for r in recs if r.monthly_delta_estimate > 0)
    md += "## Summary\n\n"
    md += common.md_table(
        ["Metric", "Value"],
        [
            ["Containers analysed", len(recs)],
            ["Over-provisioned", sum(1 for r in recs if r.status == "over")],
            ["Under-provisioned (OOM, throttling or usage above request)", sum(1 for r in recs if r.status == "under")],
            ["Within tolerance", sum(1 for r in recs if r.status == "ok")],
            ["Requested CPU that can be released (cores)", f"{-sum(r.cpu_delta_cores for r in recs if r.cpu_delta_cores < 0):.2f}"],
            ["Requested memory that can be released (GiB)", f"{-sum(r.mem_delta_gib for r in recs if r.mem_delta_gib < 0):.2f}"],
            ["Estimated monthly reduction (requests, placeholder prices)", common.fmt_money(-savings)],
            ["Estimated monthly increase for under-provisioned fixes", common.fmt_money(increases)],
            ["CPU limits policy", cpu_limits],
        ],
    )
    md += "\n## Recommendations\n\n"
    rows = []
    for r in recs:
        rows.append(
            [
                f"{r.namespace}/{r.workload}",
                r.kind,
                r.container,
                r.replicas,
                f"{r.current_cpu_request_m}m -> **{r.rec_cpu_request_m}m**",
                f"{r.current_mem_request_mi}Mi -> **{r.rec_mem_request_mi}Mi**",
                f"{r.cpu_p95_m:.0f}m / {r.cpu_p99_m:.0f}m",
                f"{r.mem_p99_mi:.0f}Mi",
                f"{r.vpa_target_cpu_m}m / {r.vpa_target_mem_mi}Mi" if r.vpa_target_cpu_m else "-",
                r.status.upper(),
                common.fmt_money(r.monthly_delta_estimate),
            ]
        )
    md += common.md_table(["Workload", "Kind", "Container", "Replicas", "CPU request", "Memory request", "CPU p95 / p99", "Mem p99", "VPA target", "Status", "Monthly delta"], rows)
    md += "\n## Notes\n\n"
    for r in recs:
        if r.notes:
            md += f"- `{r.namespace}/{r.workload}/{r.container}`: " + "; ".join(r.notes) + "\n"
    md += "\n## How to apply\n\n"
    md += (
        "1. Start with UNDER-provisioned items: these are reliability fixes, not savings.\n"
        "2. Apply OVER-provisioned changes per namespace via the generated Kustomize patch, one environment at a time.\n"
        "3. Watch `container_cpu_cfs_throttled_periods_total` and OOM events for 7 days before the next round.\n"
        "4. Re-run after HPA targets change: HPA scales on utilisation of *requests*, so lowering requests raises utilisation.\n"
    )
    return md


def render_kustomize_patch(recs: list[Recommendation]) -> str:
    """Emit a multi-document strategic merge patch (one per workload)."""
    docs: list[str] = ["# Generated by detectors/k8s_rightsizing.py - review before applying"]
    seen: set[tuple[str, str]] = set()
    for r in recs:
        if r.status == "ok":
            continue
        key = (r.namespace, r.workload)
        containers = [x for x in recs if (x.namespace, x.workload) == key and x.status != "ok"]
        if key in seen:
            continue
        seen.add(key)
        api_version = "batch/v1" if r.kind == "CronJob" else "apps/v1"
        lines = [f"apiVersion: {api_version}", f"kind: {r.kind}", "metadata:", f"  name: {r.workload}", f"  namespace: {r.namespace}", "spec:"]
        indent = "  "
        if r.kind == "CronJob":
            lines += ["  jobTemplate:", "    spec:"]
            indent = "      "
        lines += [f"{indent}template:", f"{indent}  spec:", f"{indent}    containers:"]
        for c in containers:
            lines += [
                f"{indent}      - name: {c.container}",
                f"{indent}        resources:",
                f"{indent}          requests:",
                f"{indent}            cpu: {c.rec_cpu_request_m}m",
                f"{indent}            memory: {c.rec_mem_request_mi}Mi",
                f"{indent}          limits:",
                f"{indent}            memory: {c.rec_mem_limit_mi}Mi",
            ]
            if c.rec_cpu_limit_m is not None:
                lines.append(f"{indent}            cpu: {c.rec_cpu_limit_m}m")
        docs.append("\n".join(lines))
    return "\n---\n".join(docs) + "\n"


# --------------------------------------------------------------------------- #
# Live collection (stdlib urllib; no external deps)
# --------------------------------------------------------------------------- #
def prom_query(base_url: str, query: str, timeout: int = 60) -> list[dict[str, Any]]:  # pragma: no cover
    url = f"{base_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - internal Prometheus URL
        payload = json.loads(resp.read())
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {payload}")
    return payload["data"]["result"]


def collect_from_prometheus(base_url: str, lookback: str) -> dict[str, Any]:  # pragma: no cover
    """Aggregate per-container series into the fixture shape (pod dimension collapsed by max)."""
    agg: dict[tuple[str, str, str], dict[str, Any]] = {}
    for metric, tmpl in PROM_QUERIES.items():
        for series in prom_query(base_url, tmpl.format(lookback=lookback)):
            m = series["metric"]
            key = (m.get("namespace", ""), m.get("pod", "").rsplit("-", 2)[0], m.get("container", ""))
            entry = agg.setdefault(
                key,
                {
                    "namespace": key[0],
                    "workload": key[1],
                    "kind": "Deployment",
                    "container": key[2],
                    "replicas": 0,
                    "cpu_p50_m": 0,
                    "cpu_max_m": 0,
                    "mem_max_mi": 0,
                    "oom_kills_14d": 0,
                    "throttled_ratio_p95": 0.0,
                    "cpu_limit_m": 0,
                    "mem_limit_mi": 0,
                },
            )
            value = float(series["value"][1])
            entry[metric] = max(float(entry.get(metric, 0.0)), value)
            if metric == "cpu_request_m":
                entry["replicas"] += 1
    return {"cluster": base_url, "lookback": lookback, "containers": [v for v in agg.values() if v.get("cpu_request_m") and v.get("cpu_p95_m") is not None]}


def main(argv: list[str] | None = None) -> int:
    parser = common.base_parser("Recommend container requests/limits from Prometheus usage percentiles.")
    parser.add_argument("--prometheus-url", default=os.getenv("PROMETHEUS_URL", "http://localhost:9090"))
    parser.add_argument("--lookback", default=os.getenv("PROMETHEUS_LOOKBACK", "14d"))
    parser.add_argument("--vpa-json", metavar="PATH", help="Flattened VPA recommendations to compare against.")
    parser.add_argument("--cpu-headroom", type=float, default=0.30)
    parser.add_argument("--mem-headroom", type=float, default=0.20)
    parser.add_argument("--cpu-limits", choices=["none", "2x"], default="none", help="Emit CPU limits or not (see docs/rightsizing-methodology.md).")
    parser.add_argument("--min-change-pct", type=float, default=0.15, help="Ignore changes smaller than this fraction.")
    parser.add_argument("--patch-out", metavar="PATH", help="Write Kustomize strategic-merge patch YAML here.")
    args = parser.parse_args(argv)
    common.configure_logging(args.verbose)

    pricing = common.load_pricing(args.pricing)
    if args.from_json:
        data, source = common.load_json(args.from_json), args.from_json
    elif args.dry_run:
        raise SystemExit("--dry-run requires --from-json for offline analysis.")
    else:
        data, source = collect_from_prometheus(args.prometheus_url, args.lookback), args.prometheus_url
    vpa = common.load_json(args.vpa_json) if args.vpa_json else None

    recs = analyze(data, pricing, vpa, cpu_headroom=args.cpu_headroom, mem_headroom=args.mem_headroom, cpu_limits=args.cpu_limits, min_change_pct=args.min_change_pct)
    common.write_json(args.json_out, {"cluster": data.get("cluster"), "recommendations": recs})
    if args.patch_out:
        common.write_text(args.patch_out, render_kustomize_patch(recs))
    common.write_text(args.md_out, render_markdown(data, recs, source, args.cpu_limits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
