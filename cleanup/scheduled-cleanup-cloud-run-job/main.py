#!/usr/bin/env python3
"""Cloud Run Job entrypoint: run detectors, write reports, publish a summary.

Build context is the repository root (see Dockerfile). Configuration is via
environment variables set on the Job:

  GOOGLE_CLOUD_PROJECT   target project for live collection
  FIXTURE_MODE           "true" to run against bundled fixtures (smoke tests)
  REPORT_BUCKET          gs bucket to upload Markdown/JSON reports to (optional)
  SLACK_WEBHOOK_URL      optional; posts the summary line
  CLEANUP_EXECUTE        "true" enables destructive cleanup; default is dry-run

The job never deletes anything unless CLEANUP_EXECUTE=true AND the finding
has no ``keep=true`` label AND the estimated age is above the threshold.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from detectors import bigquery_optimizer, common, idle_resources, k8s_rightsizing, redis_capacity, storage_optimizer

LOG = logging.getLogger("finops-job")
OUT = Path(os.getenv("OUT_DIR", "/tmp/out"))
# Inside the image detectors/ sits next to main.py; in the repo it is two levels up.
_HERE = Path(__file__).resolve().parent
FIXTURES = next((c for c in (_HERE / "detectors" / "fixtures", _HERE.parent.parent / "detectors" / "fixtures") if c.is_dir()), _HERE / "detectors" / "fixtures")


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def run_detectors(fixture_mode: bool, project: str) -> dict[str, Any]:
    pricing = common.load_pricing(os.getenv("PRICING_PATH"))
    summary: dict[str, Any] = {"project": project, "fixture_mode": fixture_mode, "sections": {}}

    inv = common.load_json(FIXTURES / "idle_resources.json") if fixture_mode else idle_resources.collect_live(project)
    idle = idle_resources.analyze(inv, pricing)
    common.write_text(str(OUT / "idle-resources.md"), idle_resources.render_markdown(inv, idle, "cloud-run-job"))
    common.write_json(str(OUT / "idle-resources.json"), {"findings": idle})
    summary["sections"]["idle_resources"] = {"count": len(idle), "monthly_estimate": common.total_estimate([f for f in idle if not f.details.get("excluded")])}

    prom_url = os.getenv("PROMETHEUS_URL")
    if fixture_mode or prom_url:
        usage = common.load_json(FIXTURES / "prometheus_usage.json") if fixture_mode else k8s_rightsizing.collect_from_prometheus(prom_url or "", os.getenv("PROMETHEUS_LOOKBACK", "14d"))
        recs = k8s_rightsizing.analyze(usage, pricing)
        common.write_text(str(OUT / "rightsizing.md"), k8s_rightsizing.render_markdown(usage, recs, "cloud-run-job", "none"))
        summary["sections"]["rightsizing"] = {"containers": len(recs), "over": sum(1 for r in recs if r.status == "over"), "under": sum(1 for r in recs if r.status == "under")}

    if fixture_mode:
        bq_data = common.load_json(FIXTURES / "bigquery_jobs.json")
        gcs_data = common.load_json(FIXTURES / "gcs_buckets.json")
        redis_data = common.load_json(FIXTURES / "redis_info.json")
    else:
        bq_data = bigquery_optimizer.collect_live(project, os.getenv("BQ_REGION", "asia-south1"), 30)
        gcs_data = storage_optimizer.collect_live(project)
        redis_data = None

    bq_findings, be = bigquery_optimizer.analyze(bq_data, pricing)
    common.write_text(str(OUT / "bigquery.md"), bigquery_optimizer.render_markdown(bq_data, bq_findings, be, "cloud-run-job"))
    summary["sections"]["bigquery"] = {"findings": len(bq_findings), "slot_recommendation": be.recommendation}

    plans, _ = storage_optimizer.analyze(gcs_data, pricing)
    common.write_text(str(OUT / "storage.md"), storage_optimizer.render_markdown(gcs_data, plans, "cloud-run-job", pricing))
    summary["sections"]["storage"] = {"buckets": len(plans), "monthly_saving_estimate": round(sum(p.saving for p in plans), 2)}

    if redis_data:
        plan = redis_capacity.analyze(redis_data, pricing)
        common.write_text(str(OUT / "redis.md"), redis_capacity.render_markdown(redis_data, plan, "cloud-run-job"))
        summary["sections"]["redis"] = {"recommended_gb": plan.recommended_gb, "policy": plan.recommended_policy}

    common.write_json(str(OUT / "summary.json"), summary)
    return summary


def upload_reports(bucket: str) -> None:  # pragma: no cover - needs GCS
    storage = common.guarded_import("google.cloud.storage")
    client = storage.Client()
    prefix = f"finops-reports/{common.fixed_now().strftime('%Y-%m-%d')}"
    for path in OUT.iterdir():
        client.bucket(bucket).blob(f"{prefix}/{path.name}").upload_from_filename(str(path))
        LOG.info("uploaded gs://%s/%s/%s", bucket, prefix, path.name)


def notify_slack(webhook: str, summary: dict[str, Any]) -> None:  # pragma: no cover - network
    idle = summary["sections"].get("idle_resources", {})
    text = (
        f"FinOps weekly report for `{summary['project']}`: "
        f"{idle.get('count', 0)} idle findings (~{idle.get('monthly_estimate', 0):.0f} USD/mo est.), "
        f"storage saving est. {summary['sections'].get('storage', {}).get('monthly_saving_estimate', 0):.0f} USD/mo."
    )
    req = urllib.request.Request(webhook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()  # noqa: S310 - webhook URL from env


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    fixture_mode = _env_bool("FIXTURE_MODE")
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "example-project-dev")
    OUT.mkdir(parents=True, exist_ok=True)

    summary = run_detectors(fixture_mode, project)
    LOG.info("summary: %s", json.dumps(summary["sections"]))

    if _env_bool("CLEANUP_EXECUTE"):
        LOG.warning("CLEANUP_EXECUTE=true: destructive cleanup is delegated to cleanup_orphaned_resources.sh --execute in the runbook; this job only reports.")

    if bucket := os.getenv("REPORT_BUCKET"):
        upload_reports(bucket)
    if webhook := os.getenv("SLACK_WEBHOOK_URL"):
        notify_slack(webhook, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
