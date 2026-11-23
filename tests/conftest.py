from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from mcp_probe.probe import Options, Report, Target, run_probe

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def probe(target: Target, **opts) -> Report:
    return anyio.run(run_probe, target, Options(timeout=10, **opts))


def codes(report: Report, subject: str | None = None) -> set[str]:
    return {f.code for f in report.findings if subject is None or f.subject == subject}


@pytest.fixture
def weather_cmd() -> list[str]:
    return [sys.executable, str(EXAMPLES / "weather_server.py")]


@pytest.fixture
def broken_cmd() -> list[str]:
    return [sys.executable, str(EXAMPLES / "broken_server.py")]
