#!/usr/bin/env python3
"""Detect idle and orphaned GCP resources and estimate their monthly cost.

Checks
------
* Unattached persistent disks
* Reserved but unused static external IPs
* Stopped (TERMINATED) VMs that still pay for attached disks
* Snapshots older than ``--min-snapshot-age-days``
* GKE node pools with nodes but no non-DaemonSet pods
* Forwarding rules (load balancers) with no healthy backends
* Cloud SQL instances with ~zero connections over 14 days

Resources labelled ``keep=true`` are reported but flagged as excluded so the
cleanup automation never touches them.

Usage
-----
Offline (fixture):
    python3 -m detectors.idle_resources --from-json detectors/fixtures/idle_resources.json --md-out report.md
Live (requires google-cloud-* packages and ADC credentials):
    python3 -m detectors.idle_resources --project example-project-dev --json-out out/idle.json
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from detectors import common
from detectors.common import GiB, HOURS_PER_MONTH, Finding

EXCLUDE_LABEL = ("keep", "true")


def is_excluded(labels: dict[str, str] | None) -> bool:
    return bool(labels) and labels.get(EXCLUDE_LABEL[0], "").lower() == EXCLUDE_LABEL[1]


def _disk_price(pricing: dict[str, Any], disk_type: str) -> float:
    key = disk_type.replace("-", "_") + "_gb_month"
    return float(pricing["compute"].get(key, pricing["compute"]["pd_balanced_gb_month"]))


def _tier_monthly(pricing: dict[str, Any], tier: str, storage_gb: int) -> float:
    """Approximate Cloud SQL cost from a db-custom-<vcpu>-<mem_mb> tier string."""
    try:
        _, _, vcpu, mem_mb = tier.split("-")
        vcpus, mem_gb = int(vcpu), int(mem_mb) / 1024
    except ValueError:
        vcpus, mem_gb = 2, 7.5
    sql = pricing["cloudsql"]
    compute = (vcpus * sql["db_custom_vcpu_hour"] + mem_gb * sql["db_custom_gb_hour"]) * HOURS_PER_MONTH
    return compute + storage_gb * sql["storage_ssd_gb_month"]


# --------------------------------------------------------------------------- #
# Individual checks. Each takes the inventory dict and returns findings.
# --------------------------------------------------------------------------- #
def check_unattached_disks(inv: dict[str, Any], pricing: dict[str, Any], now: dt.datetime) -> list[Finding]:
    out: list[Finding] = []
    for disk in inv.get("disks", []):
        if disk.get("users"):
            continue
        monthly = disk["size_gb"] * _disk_price(pricing, disk["type"])
        idle_days = common.age_days(disk.get("last_attach_timestamp") or disk["creation_timestamp"], now)
        excluded = is_excluded(disk.get("labels"))
        out.append(
            Finding(
                category="unattached-disk",
                resource=f"{disk['zone']}/disks/{disk['name']}",
                severity="high" if monthly > 50 and not excluded else "medium",
                monthly_cost_estimate=round(monthly, 2),
                recommendation=(
                    "Excluded by label keep=true; review owner"
                    if excluded
                    else f"Snapshot then delete (unattached {idle_days}d). Snapshot cost ~{common.fmt_money(disk['size_gb'] * pricing['compute']['snapshot_gb_month'])}/mo"
                ),
                details={"size_gb": disk["size_gb"], "type": disk["type"], "idle_days": idle_days, "excluded": excluded, "labels": disk.get("labels", {})},
            )
        )
    return out


def check_idle_addresses(inv: dict[str, Any], pricing: dict[str, Any], now: dt.datetime) -> list[Finding]:
    out: list[Finding] = []
    for addr in inv.get("addresses", []):
        if addr.get("status") != "RESERVED" or addr.get("users"):
            continue
        monthly = pricing["compute"]["static_ip_idle_hour"] * HOURS_PER_MONTH
        excluded = is_excluded(addr.get("labels"))
        out.append(
            Finding(
                category="idle-static-ip",
                resource=f"{addr['region']}/addresses/{addr['name']}",
                severity="low",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="Excluded by label keep=true" if excluded else f"Release reserved IP {addr['address']} (unused {common.age_days(addr['creation_timestamp'], now)}d)",
                details={"address": addr["address"], "excluded": excluded},
            )
        )
    return out


def check_stopped_instances(inv: dict[str, Any], pricing: dict[str, Any], now: dt.datetime, min_days: int) -> list[Finding]:
    out: list[Finding] = []
    for vm in inv.get("instances", []):
        if vm.get("status") != "TERMINATED":
            continue
        stopped_days = common.age_days(vm["last_stop_timestamp"], now) if vm.get("last_stop_timestamp") else 0
        if stopped_days < min_days:
            continue
        monthly = sum(d["size_gb"] * _disk_price(pricing, d["type"]) for d in vm.get("disks", []))
        excluded = is_excluded(vm.get("labels"))
        out.append(
            Finding(
                category="stopped-vm-with-disks",
                resource=f"{vm['zone']}/instances/{vm['name']}",
                severity="medium" if not excluded else "low",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="Excluded by label keep=true" if excluded else f"Stopped {stopped_days}d; create machine image and delete, or confirm restart plan",
                details={"machine_type": vm["machine_type"], "stopped_days": stopped_days, "excluded": excluded},
            )
        )
    return out


def check_old_snapshots(inv: dict[str, Any], pricing: dict[str, Any], now: dt.datetime, min_age_days: int) -> list[Finding]:
    out: list[Finding] = []
    for snap in inv.get("snapshots", []):
        age = common.age_days(snap["creation_timestamp"], now)
        if age < min_age_days:
            continue
        monthly = snap["storage_bytes"] / GiB * pricing["compute"]["snapshot_gb_month"]
        excluded = is_excluded(snap.get("labels"))
        out.append(
            Finding(
                category="old-snapshot",
                resource=f"snapshots/{snap['name']}",
                severity="medium" if monthly > 10 and not excluded else "low",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="Excluded by label keep=true (retention hold)"
                if excluded
                else f"{age}d old ({common.fmt_bytes(snap['storage_bytes'])}); delete or move to a snapshot schedule with retention",
                details={"age_days": age, "source_disk": snap["source_disk"], "excluded": excluded},
            )
        )
    return out


def check_empty_node_pools(inv: dict[str, Any], pricing: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    for pool in inv.get("node_pools", []):
        if pool.get("node_count", 0) == 0 or pool.get("running_pods_excluding_daemonsets", 1) != 0:
            continue
        vcpus = _machine_vcpus(pool["machine_type"])
        mem_gb = vcpus * 4  # standard shapes; good enough for an estimate
        per_node = (vcpus * pricing["compute"]["e2_vcpu_hour"] + mem_gb * pricing["compute"]["e2_gb_hour"]) * HOURS_PER_MONTH
        if pool.get("spot"):
            per_node *= 1 - pricing["compute"]["spot_discount_fraction"]
        out.append(
            Finding(
                category="empty-node-pool",
                resource=f"{pool['cluster']}/nodePools/{pool['name']}",
                severity="high",
                monthly_cost_estimate=round(per_node * pool["node_count"], 2),
                recommendation="No workload pods for 14d; enable autoscaling with min 0 or delete the pool",
                details={"node_count": pool["node_count"], "machine_type": pool["machine_type"], "autoscaling": pool.get("autoscaling")},
            )
        )
    return out


def check_lbs_without_backends(inv: dict[str, Any], pricing: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    for fr in inv.get("forwarding_rules", []):
        if fr.get("backend_service_healthy_backends", 1) > 0 or fr.get("backends_total", 0) > 0:
            continue
        monthly = pricing["compute"]["forwarding_rule_hour"] * HOURS_PER_MONTH
        out.append(
            Finding(
                category="lb-no-backends",
                resource=f"{fr['region']}/forwardingRules/{fr['name']}",
                severity="medium",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="Forwarding rule has zero backends; delete rule, target and any reserved IP",
                details={"scheme": fr["load_balancing_scheme"], "target": fr["target"]},
            )
        )
    return out


def check_unused_cloudsql(inv: dict[str, Any], pricing: dict[str, Any], max_connections: int) -> list[Finding]:
    out: list[Finding] = []
    for db in inv.get("cloudsql_instances", []):
        if db.get("state") != "RUNNABLE" or db.get("connections_p95_14d", 999) > max_connections:
            continue
        monthly = _tier_monthly(pricing, db["tier"], db["storage_gb"])
        excluded = is_excluded(db.get("labels"))
        out.append(
            Finding(
                category="unused-cloudsql",
                resource=f"{db['region']}/sql/{db['name']}",
                severity="high" if not excluded else "low",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="Excluded by label keep=true" if excluded else f"p95 connections={db['connections_p95_14d']} over 14d; export, stop (activation policy NEVER) for 30d, then delete",
                details={"tier": db["tier"], "cpu_avg_14d": db["cpu_utilization_avg_14d"], "excluded": excluded},
            )
        )
    return out


def _machine_vcpus(machine_type: str) -> int:
    try:
        return int(machine_type.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return 2


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def analyze(inv: dict[str, Any], pricing: dict[str, Any], *, min_snapshot_age_days: int = 90, min_stopped_days: int = 14, max_sql_connections: int = 1) -> list[Finding]:
    now = common.fixed_now(inv)
    findings: list[Finding] = []
    findings += check_unattached_disks(inv, pricing, now)
    findings += check_idle_addresses(inv, pricing, now)
    findings += check_stopped_instances(inv, pricing, now, min_stopped_days)
    findings += check_old_snapshots(inv, pricing, now, min_snapshot_age_days)
    findings += check_empty_node_pools(inv, pricing)
    findings += check_lbs_without_backends(inv, pricing)
    findings += check_unused_cloudsql(inv, pricing, max_sql_connections)
    return common.sort_findings(findings)


def render_markdown(inv: dict[str, Any], findings: list[Finding], source: str) -> str:
    actionable = [f for f in findings if not f.details.get("excluded")]
    excluded = [f for f in findings if f.details.get("excluded")]
    by_cat: dict[str, float] = {}
    for f in actionable:
        by_cat[f.category] = by_cat.get(f.category, 0.0) + f.monthly_cost_estimate

    md = common.report_header(f"Idle resource report: {inv.get('project', 'unknown')}", source, common.fixed_now(inv))
    md += "## Summary\n\n"
    md += common.md_table(
        ["Metric", "Value"],
        [
            ["Findings (actionable)", len(actionable)],
            ["Findings excluded by `keep=true`", len(excluded)],
            ["Estimated monthly run-rate of actionable findings", common.fmt_money(common.total_estimate(actionable))],
        ],
    )
    md += "\n### By category\n\n"
    md += common.md_table(["Category", "Count", "Est. monthly"], [[c, sum(1 for f in actionable if f.category == c), common.fmt_money(v)] for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])])
    md += "\n## Actionable findings\n\n"
    md += common.md_table(["Category", "Resource", "Severity", "Est. monthly", "Recommendation"], [f.as_row() for f in actionable])
    md += "\n## Excluded (label `keep=true`)\n\n"
    md += common.md_table(["Category", "Resource", "Severity", "Est. monthly", "Recommendation"], [f.as_row() for f in excluded])
    md += "\n## Next steps\n\n"
    md += (
        "1. Review high-severity items with resource owners (label `team`).\n"
        "2. Run `cleanup/cleanup_orphaned_resources.sh` in dry-run mode to confirm the candidate list matches.\n"
        "3. Execute cleanup with `--execute` only after snapshots/exports are verified.\n"
    )
    return md


def collect_live(project: str) -> dict[str, Any]:  # pragma: no cover - requires cloud credentials
    """Build the same inventory shape from live APIs using google-cloud-compute."""
    compute = common.guarded_import("google.cloud.compute_v1")
    inv: dict[str, Any] = {
        "project": project,
        "as_of": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "disks": [],
        "addresses": [],
        "instances": [],
        "snapshots": [],
        "node_pools": [],
        "forwarding_rules": [],
        "cloudsql_instances": [],
    }
    for _zone, scoped in compute.DisksClient().aggregated_list(project=project):
        for d in scoped.disks or []:
            inv["disks"].append(
                {
                    "name": d.name,
                    "zone": d.zone.rsplit("/", 1)[-1],
                    "type": d.type_.rsplit("/", 1)[-1],
                    "size_gb": int(d.size_gb),
                    "users": list(d.users),
                    "labels": dict(d.labels),
                    "last_attach_timestamp": d.last_attach_timestamp or None,
                    "creation_timestamp": d.creation_timestamp,
                }
            )
    for _region, scoped in compute.AddressesClient().aggregated_list(project=project):
        for a in scoped.addresses or []:
            inv["addresses"].append(
                {
                    "name": a.name,
                    "region": a.region.rsplit("/", 1)[-1],
                    "address": a.address,
                    "status": a.status,
                    "address_type": a.address_type,
                    "users": list(a.users),
                    "creation_timestamp": a.creation_timestamp,
                    "labels": dict(a.labels),
                }
            )
    for s in compute.SnapshotsClient().list(project=project):
        inv["snapshots"].append(
            {"name": s.name, "source_disk": s.source_disk.rsplit("/", 1)[-1], "storage_bytes": int(s.storage_bytes), "creation_timestamp": s.creation_timestamp, "labels": dict(s.labels)}
        )
    common.LOG.warning("Live collection covers disks/addresses/snapshots; GKE, LB and Cloud SQL collectors are left as documented extension points.")
    return inv


def main(argv: list[str] | None = None) -> int:
    parser = common.base_parser(__doc__.split("\n\n")[0])
    parser.add_argument("--min-snapshot-age-days", type=int, default=90)
    parser.add_argument("--min-stopped-days", type=int, default=14)
    parser.add_argument("--max-sql-connections", type=int, default=1, help="p95 connections at or below this count = unused")
    args = parser.parse_args(argv)
    common.configure_logging(args.verbose)

    pricing = common.load_pricing(args.pricing)
    if common.require_offline_or_live(args):
        if not args.from_json:
            raise SystemExit("--dry-run without --from-json has nothing to analyse; pass a fixture path.")
        inv = common.load_json(args.from_json)
        source = args.from_json
    else:
        inv = collect_live(args.project)
        source = f"compute API project={args.project}"

    findings = analyze(inv, pricing, min_snapshot_age_days=args.min_snapshot_age_days, min_stopped_days=args.min_stopped_days, max_sql_connections=args.max_sql_connections)
    common.write_json(args.json_out, {"project": inv.get("project"), "findings": findings, "total_monthly_estimate": common.total_estimate(findings)})
    common.write_text(args.md_out, render_markdown(inv, findings, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
