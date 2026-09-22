#!/usr/bin/env python3
"""Analyse BigQuery spend from INFORMATION_SCHEMA extracts and recommend optimisations.

Inputs (fixture or live query of INFORMATION_SCHEMA.JOBS_BY_PROJECT / TABLE_STORAGE):
* jobs   : aggregated by query hash with bytes billed, slot-ms, run count
* tables : logical bytes split active/long-term, partitioning/clustering metadata

Outputs
* Top expensive queries by bytes billed with on-demand cost estimate
* Tables without partitioning or clustering that are scanned heavily
* Partition-expiration candidates (stale staging/import tables)
* Long-term storage candidates (not modified > 90 days but still 'active')
* Slot (edition) vs on-demand break-even using average slot-hours consumed
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from detectors import common
from detectors.common import Finding

TiB = 1024**4
GB = 1_000_000_000  # BigQuery storage is billed in decimal GB


def on_demand_cost(bytes_billed: float, pricing: dict[str, Any]) -> float:
    return bytes_billed / TiB * pricing["bigquery"]["on_demand_per_tib"]


@dataclass
class SlotBreakeven:
    window_days: int
    total_slot_hours: float
    avg_slots_needed: float
    on_demand_monthly: float
    slots_standard_monthly: float
    slots_enterprise_monthly: float
    slots_enterprise_1y_monthly: float
    recommendation: str


def slot_breakeven(jobs: list[dict[str, Any]], window_days: int, pricing: dict[str, Any]) -> SlotBreakeven:
    total_slot_ms = sum(j["total_slot_ms"] for j in jobs)
    total_slot_hours = total_slot_ms / 3_600_000
    hours_in_window = window_days * 24
    avg_slots = total_slot_hours / hours_in_window
    # Reservations are bought in 100-slot increments (baseline); autoscaling adds more in 100s.
    baseline_slots = max(100, int(round(avg_slots / 100.0 + 0.5)) * 100)
    scale = 30.0 / window_days
    on_demand_monthly = sum(on_demand_cost(j["total_bytes_billed"], pricing) for j in jobs) * scale
    bq = pricing["bigquery"]
    std = baseline_slots * bq["slot_hour_standard_edition"] * common.HOURS_PER_MONTH
    ent = baseline_slots * bq["slot_hour_enterprise_edition"] * common.HOURS_PER_MONTH
    ent_1y = baseline_slots * bq["slot_hour_enterprise_1y_commit"] * common.HOURS_PER_MONTH
    cheapest = min(std, ent_1y)
    if on_demand_monthly > cheapest * 1.2:
        rec = f"Move to a {baseline_slots}-slot reservation (autoscale above baseline); on-demand exceeds slot pricing by {on_demand_monthly / cheapest - 1:.0%}"
    elif on_demand_monthly < cheapest * 0.8:
        rec = "Stay on-demand; utilisation too bursty/low for a reservation baseline"
    else:
        rec = "Near break-even; reduce bytes scanned first (partition pruning), then re-evaluate"
    return SlotBreakeven(window_days, round(total_slot_hours, 1), round(avg_slots, 1), round(on_demand_monthly, 2), round(std, 2), round(ent, 2), round(ent_1y, 2), rec)


def check_expensive_queries(jobs: list[dict[str, Any]], pricing: dict[str, Any], window_days: int, top_n: int) -> list[Finding]:
    out: list[Finding] = []
    for j in sorted(jobs, key=lambda x: -x["total_bytes_billed"])[:top_n]:
        monthly = on_demand_cost(j["total_bytes_billed"], pricing) * 30.0 / window_days
        per_run_tib = j["total_bytes_billed"] / j["run_count"] / TiB
        sql = j.get("sample_sql", "")
        hints = []
        if "SELECT *" in sql.upper():
            hints.append("avoid SELECT *")
        if j["run_count"] >= 24 * window_days:
            hints.append(f"runs {j['run_count'] / window_days:.0f}x/day: materialise into a scheduled table or BI Engine")
        if per_run_tib > 1:
            hints.append(f"{per_run_tib:.1f} TiB per run: add partition filter / require_partition_filter")
        out.append(
            Finding(
                category="expensive-query",
                resource=j["query_hash"],
                severity="high" if monthly > 500 else "medium" if monthly > 50 else "low",
                monthly_cost_estimate=round(monthly, 2),
                recommendation="; ".join(hints) or "Review query plan for pruning opportunities",
                details={"user": j["user_email"], "team": j.get("label_team"), "runs": j["run_count"], "bytes_billed": j["total_bytes_billed"], "tables": j.get("referenced_tables", [])},
            )
        )
    return out


def _looks_temporal(column: str) -> bool:
    lowered = column.lower()
    return any(tok in lowered for tok in ("date", "time", "_ts", "ts_", "day", "created", "updated", "timestamp")) or lowered in {"ts", "dt"}


def check_table_layout(tables: list[dict[str, Any]], jobs: list[dict[str, Any]], pricing: dict[str, Any], window_days: int) -> list[Finding]:
    scanned: dict[str, float] = {}
    for j in jobs:
        for t in j.get("referenced_tables", []):
            scanned[t] = scanned.get(t, 0.0) + j["total_bytes_billed"] / max(1, len(j["referenced_tables"]))
    out: list[Finding] = []
    for t in tables:
        name = t["table"]
        scan_bytes = scanned.get(name, 0.0)
        if t["total_logical_bytes"] < 10 * GB:
            continue
        filter_cols = t.get("typical_filter_columns") or []
        date_col = next((col for col in filter_cols if _looks_temporal(col)), None)
        if not t.get("partitioning") and date_col:
            monthly_scan = on_demand_cost(scan_bytes, pricing) * 30.0 / window_days
            cluster_cols = [col for col in filter_cols if col != date_col]
            out.append(
                Finding(
                    category="unpartitioned-table",
                    resource=name,
                    severity="high" if monthly_scan > 500 else "medium",
                    monthly_cost_estimate=round(monthly_scan * 0.8, 2),  # assume pruning avoids ~80% of scanned bytes
                    recommendation=f"Partition by DATE({date_col}) and set require_partition_filter=true; estimated 80% scan reduction",
                    details={"size": common.fmt_bytes(t["total_logical_bytes"]), "scanned_in_window": common.fmt_bytes(scan_bytes), "partition_column": date_col, "cluster_columns": cluster_cols},
                )
            )
        elif not t.get("partitioning") and filter_cols and scan_bytes > 0:
            # No temporal column: partitioning by integer range is possible but clustering is the safer first step.
            out.append(
                Finding(
                    category="unclustered-table",
                    resource=name,
                    severity="low",
                    monthly_cost_estimate=round(on_demand_cost(scan_bytes, pricing) * 30.0 / window_days * 0.3, 2),
                    recommendation=f"No date column to partition on; cluster by ({', '.join(filter_cols)}) or use integer-range partitioning",
                    details={"size": common.fmt_bytes(t["total_logical_bytes"]), "scanned_in_window": common.fmt_bytes(scan_bytes)},
                )
            )
        elif not t.get("clustering") and scan_bytes > TiB:
            cols = ", ".join(t.get("typical_filter_columns") or [])
            out.append(
                Finding(
                    category="unclustered-table",
                    resource=name,
                    severity="low",
                    monthly_cost_estimate=round(on_demand_cost(scan_bytes, pricing) * 30.0 / window_days * 0.3, 2),
                    recommendation=f"Add clustering on ({cols}) to cut block scans within partitions",
                    details={"scanned_in_window": common.fmt_bytes(scan_bytes)},
                )
            )
    return out


def check_partition_expiration(tables: list[dict[str, Any]], pricing: dict[str, Any], stale_days: int) -> list[Finding]:
    out: list[Finding] = []
    for t in tables:
        if t.get("partitioning") and t.get("partition_expiration_days") is None and t["last_modified_days_ago"] >= stale_days:
            monthly = t["active_logical_bytes"] / GB * pricing["bigquery"]["active_storage_gb_month"]
            out.append(
                Finding(
                    category="partition-expiration",
                    resource=t["table"],
                    severity="medium",
                    monthly_cost_estimate=round(monthly, 2),
                    recommendation=f"Not modified for {t['last_modified_days_ago']}d; set partition_expiration_days (e.g. 180) or drop",
                    details={"size": common.fmt_bytes(t["total_logical_bytes"])},
                )
            )
        elif t.get("partitioning") and t.get("partition_expiration_days") is None and t["total_logical_bytes"] > 10 * TiB:
            lt_share = t.get("long_term_logical_bytes", 0) / t["total_logical_bytes"]
            out.append(
                Finding(
                    category="partition-expiration",
                    resource=t["table"],
                    severity="low",
                    monthly_cost_estimate=round(t.get("long_term_logical_bytes", 0) / GB * pricing["bigquery"]["long_term_storage_gb_month"] * 0.5, 2),
                    recommendation=f"{lt_share:.0%} of partitions are long-term; agree a retention window with owners and set partition expiration",
                    details={"size": common.fmt_bytes(t["total_logical_bytes"])},
                )
            )
    return out


def check_long_term_candidates(tables: list[dict[str, Any]], pricing: dict[str, Any]) -> list[Finding]:
    """Tables edited rarely but kept active by small appends: split hot/cold so cold partitions age into long-term pricing."""
    out: list[Finding] = []
    bq = pricing["bigquery"]
    for t in tables:
        active_gb = t["active_logical_bytes"] / GB
        if not t.get("partitioning") and active_gb > 1000 and t["last_modified_days_ago"] <= 7:
            saving = active_gb * 0.7 * (bq["active_storage_gb_month"] - bq["long_term_storage_gb_month"])
            out.append(
                Finding(
                    category="long-term-storage",
                    resource=t["table"],
                    severity="low",
                    monthly_cost_estimate=round(saving, 2),
                    recommendation="Unpartitioned table is rewritten on every load so nothing ages into long-term storage; partition it so untouched partitions convert after 90 days",
                    details={"active": common.fmt_bytes(t["active_logical_bytes"])},
                )
            )
    return out


def analyze(data: dict[str, Any], pricing: dict[str, Any], *, top_n: int = 5, stale_days: int = 60) -> tuple[list[Finding], SlotBreakeven]:
    window = int(data.get("window_days", 30))
    jobs, tables = data.get("jobs", []), data.get("tables", [])
    findings: list[Finding] = []
    findings += check_expensive_queries(jobs, pricing, window, top_n)
    findings += check_table_layout(tables, jobs, pricing, window)
    findings += check_partition_expiration(tables, pricing, stale_days)
    findings += check_long_term_candidates(tables, pricing)
    return common.sort_findings(findings), slot_breakeven(jobs, window, pricing)


def render_markdown(data: dict[str, Any], findings: list[Finding], be: SlotBreakeven, source: str) -> str:
    md = common.report_header(f"BigQuery optimisation report: {data.get('project', 'project')} ({data.get('region', '')})", source, common.fixed_now(data))
    md += "## Slot vs on-demand break-even\n\n"
    md += common.md_table(
        ["Metric", "Value"],
        [
            ["Window (days)", be.window_days],
            ["Total slot-hours consumed", be.total_slot_hours],
            ["Average concurrent slots", be.avg_slots_needed],
            ["On-demand monthly (extrapolated)", common.fmt_money(be.on_demand_monthly)],
            ["Standard edition baseline monthly", common.fmt_money(be.slots_standard_monthly)],
            ["Enterprise edition PAYG monthly", common.fmt_money(be.slots_enterprise_monthly)],
            ["Enterprise 1y commit monthly", common.fmt_money(be.slots_enterprise_1y_monthly)],
            ["Recommendation", be.recommendation],
        ],
    )
    md += "\n## Findings\n\n"
    md += common.md_table(["Category", "Resource", "Severity", "Est. monthly impact", "Recommendation"], [f.as_row() for f in findings])
    md += "\n## Suggested DDL\n\n```sql\n"
    for f in findings:
        if f.category == "unpartitioned-table":
            cluster = ", ".join(f.details.get("cluster_columns") or []) or None
            md += f"-- {f.resource}\nCREATE TABLE `{f.resource}_partitioned`\nPARTITION BY DATE({f.details['partition_column']})\n"
            if cluster:
                md += f"CLUSTER BY {cluster}\n"
            md += f"OPTIONS (require_partition_filter = TRUE)\nAS SELECT * FROM `{f.resource}`;\n\n"
        if f.category == "partition-expiration":
            md += f"ALTER TABLE `{f.resource}` SET OPTIONS (partition_expiration_days = 180);\n\n"
    md += "```\n"
    return md


def collect_live(project: str, region: str, window_days: int) -> dict[str, Any]:  # pragma: no cover
    bigquery = common.guarded_import("google.cloud.bigquery")
    client = bigquery.Client(project=project)
    jobs_sql = f"""
      SELECT
        TO_HEX(MD5(REGEXP_REPLACE(query, r'\\s+', ' '))) AS query_hash,
        ANY_VALUE(user_email) AS user_email,
        (SELECT value FROM UNNEST(labels) WHERE key = 'team' LIMIT 1) AS label_team,
        COUNT(*) AS run_count,
        SUM(total_bytes_billed) AS total_bytes_billed,
        SUM(total_slot_ms) AS total_slot_ms,
        AVG(TIMESTAMP_DIFF(end_time, start_time, SECOND)) AS avg_duration_s,
        ANY_VALUE(query) AS sample_sql
      FROM `{project}.region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
      WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_days} DAY)
        AND job_type = 'QUERY' AND state = 'DONE' AND error_result IS NULL
      GROUP BY query_hash, label_team
      ORDER BY total_bytes_billed DESC
      LIMIT 200
    """
    jobs = [dict(row) for row in client.query(jobs_sql).result()]
    for j in jobs:
        j.setdefault("referenced_tables", [])
    return {"project": project, "region": region, "window_days": window_days, "jobs": jobs, "tables": []}


def main(argv: list[str] | None = None) -> int:
    parser = common.base_parser("Analyse BigQuery jobs and table layout for cost optimisation.")
    parser.add_argument("--region", default="asia-south1")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--stale-days", type=int, default=60)
    args = parser.parse_args(argv)
    common.configure_logging(args.verbose)
    pricing = common.load_pricing(args.pricing)

    if common.require_offline_or_live(args):
        if not args.from_json:
            raise SystemExit("--dry-run requires --from-json.")
        data, source = common.load_json(args.from_json), args.from_json
    else:
        data, source = collect_live(args.project, args.region, args.window_days), f"INFORMATION_SCHEMA project={args.project}"

    findings, be = analyze(data, pricing, top_n=args.top_n, stale_days=args.stale_days)
    common.write_json(args.json_out, {"project": data.get("project"), "findings": findings, "slot_breakeven": be})
    common.write_text(args.md_out, render_markdown(data, findings, be, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
