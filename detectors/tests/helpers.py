"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from detectors import common

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name: str) -> Any:
    return common.load_json(FIXTURES / name)


def pricing() -> dict[str, Any]:
    return common.load_pricing(None)
