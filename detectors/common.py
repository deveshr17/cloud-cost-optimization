"""Shared helpers for the detectors.

Everything in this module is standard-library only so that every detector can
run offline against JSON fixtures. Google client libraries are imported lazily
(and guarded) only inside the live-collection code paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

LOG = logging.getLogger("finops")

GiB = 1024**3
HOURS_PER_MONTH = 730.0


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #
def base_parser(description: str) -> argparse.ArgumentParser:
    """Return an argparse parser with the flags shared by every detector."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="Read inventory/metrics from a JSON fixture instead of live APIs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for offline mode: never touch cloud APIs (implied by --from-json).",
    )
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""), help="GCP project ID (live mode).")
    parser.add_argument("--json-out", metavar="PATH", help="Write machine-readable findings JSON here.")
    parser.add_argument("--md-out", metavar="PATH", help="Write a Markdown report here (default: stdout).")
    parser.add_argument("--pricing", metavar="PATH", default=None, help="Pricing JSON (defaults to detectors/pricing.example.json).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def require_offline_or_live(args: argparse.Namespace) -> bool:
    """Return True when offline mode should be used.

    Live mode requires a project; we fail early with a clear message rather
    than a stack trace from a missing client library.
    """
    if args.from_json or args.dry_run:
        return True
    if not args.project:
        raise SystemExit("Live mode needs --project or GOOGLE_CLOUD_PROJECT (or use --from-json for offline mode).")
    return False


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def load_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_pricing(path: str | None) -> dict[str, Any]:
    """Load unit prices. These are placeholders; replace with the Cloud Billing Catalog API output."""
    candidate = Path(path) if path else Path(__file__).with_name("pricing.example.json")
    return load_json(candidate)


def write_text(path: str | None, content: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        LOG.info("wrote %s", path)
    else:
        sys.stdout.write(content)


def write_json(path: str | None, payload: Any) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(to_plain(payload), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    LOG.info("wrote %s", path)


def to_plain(obj: Any) -> Any:
    """Recursively convert dataclasses to dicts so they serialise cleanly."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# Findings model
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    """A single actionable recommendation."""

    category: str
    resource: str
    severity: str  # low | medium | high
    monthly_cost_estimate: float
    recommendation: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> list[str]:
        return [
            self.category,
            f"`{self.resource}`",
            self.severity,
            fmt_money(self.monthly_cost_estimate),
            self.recommendation,
        ]


def total_estimate(findings: Iterable[Finding]) -> float:
    return round(sum(f.monthly_cost_estimate for f in findings), 2)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: (order.get(f.severity, 9), -f.monthly_cost_estimate, f.resource))


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fmt_money(value: float, currency: str = "USD") -> str:
    return f"{value:,.2f} {currency}"


def fmt_bytes(num: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    idx = 0
    value = float(num)
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:,.1f} {units[idx]}"


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render a GitHub-flavoured Markdown table."""
    rows = list(rows)
    if not rows:
        return "_No rows._\n"
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return f"{head}\n{sep}\n{body}\n"


def report_header(title: str, source: str, generated_at: dt.datetime | None = None) -> str:
    ts = (generated_at or dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# {title}\n\n"
        f"Generated: {ts}  \n"
        f"Source: `{source}`\n\n"
        "> All cost figures are **estimates computed from placeholder unit prices on synthetic data**. "
        "They illustrate the calculation method and are not realised savings.\n\n"
    )


def fixed_now(fixture: dict[str, Any] | None = None) -> dt.datetime:
    """Detectors take 'now' from the fixture when present so reports are reproducible."""
    if fixture and fixture.get("as_of"):
        return parse_ts(fixture["as_of"])
    return dt.datetime.now(tz=dt.timezone.utc)


def parse_ts(value: str) -> dt.datetime:
    value = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_days(ts: str, now: dt.datetime) -> int:
    return max(0, (now - parse_ts(ts)).days)


# --------------------------------------------------------------------------- #
# Statistics (stdlib only)
# --------------------------------------------------------------------------- #
def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile; deterministic and dependency-free."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def linear_trend(values: Sequence[float]) -> float:
    """Least-squares slope per sample; positive means growth."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    var = sum((x - mean_x) ** 2 for x in xs)
    return cov / var if var else 0.0


def round_up_cpu_millicores(m: float, step: int = 50) -> int:
    return int(math.ceil(m / step) * step) if m > 0 else step


def round_up_mebibytes(mi: float, step: int = 32) -> int:
    return int(math.ceil(mi / step) * step) if mi > 0 else step


def guarded_import(module: str) -> Any:
    """Import a Google client lazily with a helpful error if it is missing."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover - only in live mode
        raise SystemExit(f"{module} is required for live mode. Install with `pip install -r requirements.txt` or run with --from-json for offline analysis.") from exc
