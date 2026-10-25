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
from typing import Any, cast

from aeos.verification.junit import JUnitParseError, JUnitReport, parse_junit
from aeos.verification.procs import ProcResult


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
    # The verifier's result seal (``aeos.verification.seal``, review W6-054) for steps that run candidate code:
    # ``seal_step(name)`` opens a step's channel, ``check_seal(step_seal, res, junit, report, label=...)`` cross-checks a
    # sealed pytest step and records its result (and any tamper finding) on the verification. Both are set by the
    # verifier only; a context built without them (unit tests with a scripted runner) runs steps unsealed and unnamed.
    seal_step: Callable[[str], Any] | None = None
    check_seal: Callable[..., Any] | None = None


def run_named(ctx: StepContext, name: str, args: list[str], *, pythonpath: Path, autoload: bool,
              timeout: float | None = None) -> ProcResult:
    """A non-pytest step through the runner, named in the verifier's step list (kill-group survivors are attributed
    to it, ``aeos.verification.runner``)."""
    kw: dict[str, Any] = {"step": name} if ctx.seal_step is not None else {}
    return cast(ProcResult, ctx.runner.python(args, pythonpath=pythonpath, autoload=autoload,
                                              timeout=ctx.timeout if timeout is None else timeout, **kw))


def run_pytest(ctx: StepContext, name: str, args: list[str], *, pythonpath: Path, autoload: bool, junit: Path,
               label: str, runner: Any = None) -> tuple[ProcResult, JUnitReport | None, str | None, list[str]]:
    """One pytest step that runs candidate code (``args`` start with ``-m pytest``), under the verifier's result seal
    when the verifier provides it. Returns the process result, the parsed junit report, the parse error and the seal
    problems: a non-empty list means the step's result cannot be trusted and the step must not pass (a forged or
    inconsistent result is also recorded as a tamper finding by ``check_seal``: the run is QUARANTINED)."""
    rn = runner if runner is not None else ctx.runner
    step_seal = ctx.seal_step(name) if ctx.seal_step is not None else None
    if step_seal is None:
        res = rn.python(args, pythonpath=pythonpath, autoload=autoload, timeout=ctx.timeout)
        report, parse_error = parse_report(junit)
        return res, report, parse_error, []
    from aeos.verification.seal import PLUGIN_MODULE

    sealed_args = [*args[:2], "-p", PLUGIN_MODULE, *args[2:]] if args[:2] == ["-m", "pytest"] else args
    res = rn.python(sealed_args, pythonpath=pythonpath, autoload=autoload, timeout=ctx.timeout, seal=step_seal,
                    step=name)
    report, parse_error = parse_report(junit)
    sealed = ctx.check_seal(step_seal, res, junit, report, label=label) if ctx.check_seal is not None else None
    problems = list(sealed.problems) if sealed is not None and sealed.status != "PASS" else []
    return res, report, parse_error, problems


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
