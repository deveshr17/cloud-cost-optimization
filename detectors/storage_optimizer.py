#!/usr/bin/env python3
"""Recommend GCS lifecycle rules and storage-class transitions from object age distributions.

Model
-----
For each bucket we know how many bytes sit in each age band (0-30, 30-90,
90-365, 365+ days) and the 30-day access pattern. We compute:

* current monthly cost           = bytes * class price + ops + retrieval
* proposed lifecycle policy      = Standard -> Nearline (30d) -> Coldline (90d) -> Archive (365d)
                                   unless the access pattern says objects are still read
* noncurrent version cleanup     = delete noncurrent versions older than N days / keep N newest
* retrieval-risk guard           = skip a transition if egress/reads would make
                                   retrieval fees exceed the storage saving

Unit prices come from ``pricing.example.json`` (placeholders).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from detectors import common
from detectors.common import Finding

GB = 1_000_000_000
BANDS = ["0-30", "30-90", "90-365", "365+"]
CLASS_PRICE_KEY = {"STANDARD": "standard_gb_month", "NEARLINE": "nearline_gb_month", "COLDLINE": "coldline_gb_month", "ARCHIVE": "archive_gb_month"}
RETRIEVAL_KEY = {"STANDARD": None, "NEARLINE": "nearline_retrieval_gb", "COLDLINE": "coldline_retrieval_gb", "ARCHIVE": "archive_retrieval_gb"}
MIN_DURATION_DAYS = {"STANDARD": 0, "NEARLINE": 30, "COLDLINE": 90, "ARCHIVE": 365}


@dataclass
class LifecycleRule:
    action: str
    condition: dict[str, Any]
    storage_class: str | None = None

    def to_gcs(self) -> dict[str, Any]:
        rule: dict[str, Any] = {"action": {"type": self.action}, "condition": self.condition}
        if self.storage_class:
            rule["action"]["storageClass"] = self.storage_class
        return rule


@dataclass
class BucketPlan:
    bucket: str
    current_class: str
    total_bytes: int
    current_monthly: float
    proposed_monthly: float
    rules: list[LifecycleRule] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def saving(self) -> float:
        return round(self.current_monthly - self.proposed_monthly, 2)


def band_class_plan(access: dict[str, Any]) -> dict[str, str]:
    """Decide the target class for each age band given how hot the data is."""
    hot_reads = access.get("class_b_ops_30d", 0) > 10_000_000 or access.get("egress_gb_30d", 0) > 1000
    warm_reads = access.get("last_read_age_days_p50", 999) <= 30
    if hot_reads:
        # Serving bucket (CDN origin etc): never downgrade
        return {b: "STANDARD" for b in BANDS}
    if warm_reads:
        return {"0-30": "STANDARD", "30-90": "STANDARD", "90-365": "NEARLINE", "365+": "COLDLINE"}
    return {"0-30": "STANDARD", "30-90": "NEARLINE", "90-365": "COLDLINE", "365+": "ARCHIVE"}


def monthly_storage_cost(bytes_by_class: dict[str, float], pricing: dict[str, Any]) -> float:
    gcs = pricing["gcs"]
    return sum(b / GB * gcs[CLASS_PRICE_KEY[cls]] for cls, b in bytes_by_class.items())


def ops_cost(access: dict[str, Any], pricing: dict[str, Any]) -> float:
    gcs = pricing["gcs"]
    return access.get("class_a_ops_30d", 0) / 10_000 * gcs["class_a_ops_per_10k"] + access.get("class_b_ops_30d", 0) / 10_000 * gcs["class_b_ops_per_10k"]


def plan_bucket(bucket: dict[str, Any], pricing: dict[str, Any], *, noncurrent_days: int = 30, keep_versions: int = 3) -> BucketPlan:
    gcs = pricing["gcs"]
    access = bucket.get("access_pattern", {})
    dist = bucket["age_distribution_bytes"]
    total = int(sum(dist.values())) + int(bucket.get("noncurrent_bytes", 0))
    current_class = bucket["storage_class"]

    current_cost = monthly_storage_cost({current_class: sum(dist.values()) + bucket.get("noncurrent_bytes", 0)}, pricing) + ops_cost(access, pricing)
    # Existing lifecycle rules reduce actual cost; we model the bucket as-is for the baseline but note it.
    notes: list[str] = []
    if bucket.get("lifecycle_rules"):
        notes.append(f"{len(bucket['lifecycle_rules'])} lifecycle rule(s) already present; proposal replaces them")

    target = band_class_plan(access)
    proposed_bytes: dict[str, float] = {}
    for band in BANDS:
        cls = target[band]
        # Never move to a class 'warmer' than the bucket default (e.g. NEARLINE bucket stays >= NEARLINE)
        if MIN_DURATION_DAYS[cls] < MIN_DURATION_DAYS[current_class]:
            cls = current_class
        proposed_bytes[cls] = proposed_bytes.get(cls, 0.0) + dist[band]

    # Retrieval-risk guard: estimate monthly retrieval on colder bytes proportional to egress
    retrieval = 0.0
    read_gb = access.get("egress_gb_30d", 0)
    cold_fraction = sum(b for c, b in proposed_bytes.items() if c != "STANDARD") / max(1, sum(proposed_bytes.values()))
    for cls, b in proposed_bytes.items():
        key = RETRIEVAL_KEY[cls]
        if key:
            retrieval += read_gb * cold_fraction * (b / max(1, sum(proposed_bytes.values()))) * gcs[key]

    proposed_cost = monthly_storage_cost(proposed_bytes, pricing) + ops_cost(access, pricing) + retrieval
    rules: list[LifecycleRule] = []
    band_start = {"0-30": 0, "30-90": 30, "90-365": 90, "365+": 365}
    colder_order = ["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"]
    for cls in ("NEARLINE", "COLDLINE", "ARCHIVE"):
        if cls not in proposed_bytes or cls == current_class:
            continue
        starts = [band_start[b] for b, c in target.items() if c == cls]
        # Objects may transition from the bucket default class or any warmer class already applied.
        from_classes = [c for c in colder_order if colder_order.index(current_class) <= colder_order.index(c) < colder_order.index(cls)]
        rules.append(LifecycleRule("SetStorageClass", {"age": min(starts), "matchesStorageClass": from_classes}, storage_class=cls))
    noncurrent = float(bucket.get("noncurrent_bytes", 0))
    if bucket.get("versioning") and noncurrent > 0:
        # Assume ~80% of noncurrent bytes are older than the retention window; the rest stays in the bucket class.
        proposed_cost += noncurrent * 0.2 / GB * gcs[CLASS_PRICE_KEY[current_class]]
        rules.append(LifecycleRule("Delete", {"isLive": False, "daysSinceNoncurrentTime": noncurrent_days}))
        rules.append(LifecycleRule("Delete", {"isLive": False, "numNewerVersions": keep_versions}))
        notes.append(f"{common.fmt_bytes(bucket['noncurrent_bytes'])} of noncurrent versions; ~80% assumed deletable after {noncurrent_days}d/keep {keep_versions}")
    if noncurrent > 0 and not (bucket.get("versioning") and noncurrent > 0):
        proposed_cost += noncurrent / GB * gcs[CLASS_PRICE_KEY[current_class]]
    if retrieval > 0:
        notes.append(f"Retrieval fees modelled at {common.fmt_money(retrieval)}/mo from {read_gb} GB reads")
    if target == {b: "STANDARD" for b in BANDS}:
        notes.append("Hot serving bucket: no class transitions recommended; consider Cloud CDN to reduce egress")
    if any(not bucket.get("labels", {}).get(k) for k in ("env", "team")):
        notes.append("Missing required labels (env/team); showback attribution incomplete")

    return BucketPlan(bucket["name"], current_class, total, round(current_cost, 2), round(max(0.0, proposed_cost), 2), rules, notes)


def analyze(data: dict[str, Any], pricing: dict[str, Any], **kw: Any) -> tuple[list[BucketPlan], list[Finding]]:
    plans = [plan_bucket(b, pricing, **kw) for b in data["buckets"]]
    findings = [
        Finding(
            category="gcs-lifecycle",
            resource=p.bucket,
            severity="high" if p.saving > 100 else "medium" if p.saving > 10 else "low",
            monthly_cost_estimate=p.saving,
            recommendation=f"Apply {len(p.rules)} lifecycle rule(s)" if p.rules else "No change",
            details={"rules": [r.to_gcs() for r in p.rules], "notes": p.notes},
        )
        for p in plans
    ]
    return sorted(plans, key=lambda p: -p.saving), common.sort_findings(findings)


def render_markdown(data: dict[str, Any], plans: list[BucketPlan], source: str, pricing: dict[str, Any]) -> str:
    md = common.report_header(f"Cloud Storage optimisation report: {data.get('project', 'project')}", source, common.fixed_now(data))
    md += "## Unit prices used (placeholders, USD per GB-month)\n\n"
    gcs = pricing["gcs"]
    md += common.md_table(
        ["Class", "Storage", "Retrieval per GB", "Min duration"], [[c, gcs[CLASS_PRICE_KEY[c]], gcs[RETRIEVAL_KEY[c]] if RETRIEVAL_KEY[c] else 0, f"{MIN_DURATION_DAYS[c]}d"] for c in CLASS_PRICE_KEY]
    )
    md += "\n## Bucket plans\n\n"
    md += common.md_table(
        ["Bucket", "Class", "Size", "Current monthly", "Proposed monthly", "Saving", "Rules"],
        [
            [f"`{p.bucket}`", p.current_class, common.fmt_bytes(p.total_bytes), common.fmt_money(p.current_monthly), common.fmt_money(p.proposed_monthly), common.fmt_money(p.saving), len(p.rules)]
            for p in plans
        ],
    )
    md += f"\n**Total estimated monthly saving:** {common.fmt_money(sum(p.saving for p in plans))}\n\n"
    md += "## Proposed lifecycle JSON\n\n"
    for p in plans:
        if not p.rules:
            continue
        md += f"### `{p.bucket}`\n\n"
        for n in p.notes:
            md += f"- {n}\n"
        md += "\n```json\n" + json.dumps({"lifecycle": {"rule": [r.to_gcs() for r in p.rules]}}, indent=2) + "\n```\n\n"
        md += f"Apply: `gcloud storage buckets update gs://{p.bucket} --lifecycle-file=lifecycle-{p.bucket}.json`\n\n"
    return md


def collect_live(project: str) -> dict[str, Any]:  # pragma: no cover
    storage = common.guarded_import("google.cloud.storage")
    client = storage.Client(project=project)
    buckets = []
    for b in client.list_buckets():
        buckets.append(
            {
                "name": b.name,
                "location": b.location,
                "storage_class": b.storage_class,
                "versioning": b.versioning_enabled,
                "lifecycle_rules": list(b.lifecycle_rules),
                "labels": dict(b.labels or {}),
                "access_pattern": {},
                "age_distribution_bytes": {band: 0 for band in BANDS},
                "noncurrent_bytes": 0,
            }
        )
    common.LOG.warning("Live mode lists buckets only; populate age_distribution_bytes from a Storage Insights inventory report.")
    return {"project": project, "buckets": buckets}


def main(argv: list[str] | None = None) -> int:
    parser = common.base_parser("Recommend GCS lifecycle rules and storage class transitions.")
    parser.add_argument("--noncurrent-days", type=int, default=30)
    parser.add_argument("--keep-versions", type=int, default=3)
    args = parser.parse_args(argv)
    common.configure_logging(args.verbose)
    pricing = common.load_pricing(args.pricing)

    if common.require_offline_or_live(args):
        if not args.from_json:
            raise SystemExit("--dry-run requires --from-json.")
        data, source = common.load_json(args.from_json), args.from_json
    else:
        data, source = collect_live(args.project), f"storage API project={args.project}"

    plans, findings = analyze(data, pricing, noncurrent_days=args.noncurrent_days, keep_versions=args.keep_versions)
    common.write_json(args.json_out, {"project": data.get("project"), "plans": plans, "findings": findings})
    common.write_text(args.md_out, render_markdown(data, plans, source, pricing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
