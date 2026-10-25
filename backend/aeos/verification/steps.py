"""Shared shapes for the verifier's impact-selected steps (§16, §22, §31; wave 7 E1).

Each step module (``controlled``, ``adversarial``, ``contracts``, ``envs``, ``frontend``, ``db_evidence``) takes a
``StepContext`` and returns a ``StepResult``: one named check with its status, the evidence to collect and the
lines the verifier adds to the report. The verifier applies the result (``_Verifier._apply_step``); a step never
writes evidence or touches the outcome itself. Statuses follow the verifier vocabulary: PASS | FAIL | NOT_RUN |
NOT_PROVEN | UNKNOWN | NOT_APPLICABLE. A step that could not run states why (NOT_RUN/NOT_PROVEN with the concrete
reason) and is never reported as PASS.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.verification.junit import JUnitParseError, JUnitReport, parse_junit


@dataclass
class StepEvidence:
    kind: str
    claim: str
    content: dict[str, Any] | str | bytes | None
    level: str = "E2"
    check: str | None = None
    env_extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    check: str
    status: str
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    evidence: list[StepEvidence] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # failures: the verification FAILs
    not_proven: list[str] = field(default_factory=list)  # stated on the report
    blocking: list[str] = field(default_factory=list)  # stated AND keep the result from PASS


@dataclass
class StepContext:
    """What a step may use. ``runner`` executes candidate code (inside the verifier's tamper window)."""

    runner: Any
    snapshot: Path
    workdir: Path
    diff_text: str = ""
    changed: list[str] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    timeout: float = 900.0
    suite_kind: str = "CONTROLLER"
    is_platform: bool = False
    role: str = ""
    ini: Path | None = None
    trusted_copy: Path | None = None
    expected: list[str] = field(default_factory=list)
    trusted_cases: dict[str, str] = field(default_factory=dict)  # junit key -> outcome of the trusted run
    base_tree: Callable[[], Path | None] = lambda: None  # lazily materialised recorded base commit
    basetemp: Callable[[str], list[str]] = lambda name: []
    session: Any = None
    project: Any = None


def pack_text(name: str) -> str:
    """Source of a verifier-owned pack/driver (``aeos/verification/packs/<name>``) from the trusted controller."""
    return importlib.resources.files("aeos.verification.packs").joinpath(name).read_text(encoding="utf-8")


def parse_report(path: Path) -> tuple[JUnitReport | None, str | None]:
    try:
        return parse_junit(path), None
    except JUnitParseError as exc:
        return None, str(exc)


def last_json_line(stdout: str | None) -> dict[str, Any]:
    import json

    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            return data if isinstance(data, dict) else {}
    return {}


def outcomes_for(cases: dict[str, str], prefixes: list[str]) -> dict[str, int]:
    """passed/failed/skipped counts of the junit cases whose key starts with one of *prefixes*."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for key, outcome in cases.items():
        if any(key.startswith(p) for p in prefixes):
            bucket = "failed" if outcome in ("failed", "error") else ("skipped" if outcome == "skipped" else "passed")
            counts[bucket] += 1
    return counts
