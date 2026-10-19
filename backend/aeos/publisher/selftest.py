"""Publication idempotency self-test (§08 Recovery "duplicate-publication prevention", §14, §29, §50 1.20/1.21).

``aeos publish idempotency-test`` runs the publisher's own timeout / partial-failure / replay tests at the
current commit against loopback fakes (respx / in-memory providers — never a real tenant, never real
credentials: every test isolates its data dir and secret store). Only when every test passes is a trusted
``PUBLICATION_IDEMPOTENCY_TEST`` EvidenceArtifact recorded (origin ``collector``), which the Doctor check
``recovery.publication_idempotency`` reads. A failing or unrunnable run records nothing that could read as proof.

Scope of the claim: the duplicate-prevention MECHANISM of this installation (outbox idempotency keys,
reconcile-by-marker before retry, partial success kept) at this commit. It says nothing about a live
Google/GitHub/Atlassian tenant's behaviour (that stays an owner pilot, §51 P2).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked
from aeos.core.paths import repo_root
from aeos.core.redaction import redact_text

EVIDENCE_KIND = "PUBLICATION_IDEMPOTENCY_TEST"
COLLECTOR_IDENTITY = "aeos-publisher-idempotency-selftest"
TIMEOUT_S = 900
TESTS: tuple[str, ...] = (
    "tests/acceptance/test_publication.py::test_provider_timeout_after_create_is_reconciled_before_retry",
    "tests/acceptance/test_publication.py::test_one_failing_target_is_partial_and_successes_are_kept",
    "tests/unit/test_publisher_jobs.py::test_timeout_becomes_unknown_then_reconciled_without_duplicate",
    "tests/unit/test_publisher_jobs.py::test_unknown_with_absent_resource_is_retried_once_reconciled",
    "tests/unit/test_publisher_jobs.py::test_partial_failure_keeps_successes_and_retry_does_not_recreate",
    "tests/unit/test_publisher_google.py::test_timeout_after_create_is_reconciled_without_duplicate_file",
)


@dataclass
class RunOutcome:
    returncode: int | None
    output: str
    error: str = ""


Runner = Callable[[list[str], str], RunOutcome]


def _default_runner(argv: list[str], cwd: str) -> RunOutcome:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TIMEOUT_S, env=dict(os.environ), **kwargs)
    except subprocess.TimeoutExpired:
        return RunOutcome(None, "", f"timeout after {TIMEOUT_S}s")
    except OSError as exc:
        return RunOutcome(None, "", f"cannot start pytest: {exc.__class__.__name__}")
    return RunOutcome(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _commit(cwd: str) -> str | None:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and len(sha) in (40, 64) else None


# The code the self-test exercises: uncommitted changes here mean the evidence could not name the code it ran.
CODE_PATHS: tuple[str, ...] = ("backend", "tests")


def _uncommitted(cwd: str) -> list[str] | None:
    """Uncommitted (modified/untracked) paths under CODE_PATHS; None when git cannot tell (treated as unclean)."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal", "--", *CODE_PATHS],
                              cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line[3:].strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def run_idempotency_selftest(session: Session, *, actor: str, runner: Runner | None = None,
                             commit: str | None = None, uncommitted: list[str] | None = None) -> dict[str, Any]:
    root = repo_root()
    missing = [t for t in TESTS if not (root / t.split("::", 1)[0]).is_file()]
    if missing:
        raise Blocked("idempotency self-test files are not part of this installation", code="SELFTEST_UNAVAILABLE",
                      details={"missing": missing})
    argv = [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider", "-p", "no:warnings"]
    out = (runner or _default_runner)(argv, str(root))
    commit = commit or _commit(str(root))
    dirty = uncommitted if uncommitted is not None else _uncommitted(str(root))
    tail = redact_text("\n".join((out.output or out.error).strip().splitlines()[-15:]), pii=True)
    passed = out.returncode == 0
    result: dict[str, Any] = {"status": "PASSED" if passed else "FAILED", "returncode": out.returncode,
                              "tests": list(TESTS), "commit": commit, "summary": tail, "evidence_id": None}
    if passed and commit and dirty:
        # Evidence honesty: the run exercised working-tree code that differs from the commit it would name.
        result["status"] = "NOT_RECORDED"
        result["reason"] = ("uncommitted changes under backend/ or tests/ (" + ", ".join(dirty[:5]) + ") — the "
                            "evidence must name the exact code it ran; commit or stash, then run again")
    elif passed and commit and dirty is None:
        result["status"] = "NOT_RECORDED"
        result["reason"] = "git status unavailable: cannot prove the tests ran the code of commit " + commit[:12]
    elif passed and commit:
        from aeos.verification import facade as verification

        ev = verification.collect_evidence(
            session, kind=EVIDENCE_KIND, origin="collector", collector_identity=COLLECTOR_IDENTITY,
            claim=f"Publication duplicate prevention (timeout/partial/replay, {len(TESTS)} tests) passed at "
                  f"{commit[:12]} against loopback fakes",
            content={"tests": list(TESTS), "returncode": out.returncode, "summary": tail, "commit": commit,
                     "scope": "outbox idempotency + reconcile-by-marker + partial success; loopback fakes only"},
            evidence_level="E2", subject_type="check", subject_id="recovery.publication_idempotency",
            environment={"commit": commit, "runner": "pytest", "network": "loopback-fakes-only"})
        result["evidence_id"] = ev.id
    elif passed:
        result["status"] = "NOT_RECORDED"
        result["reason"] = "commit unknown (git rev-parse failed): evidence must name the exact commit"
    audit.record(session, actor_type="user" if actor != "local-operator" else "cli", actor_id=actor,
                 action="publication.idempotency_selftest", result="SUCCESS" if result["evidence_id"] else "FAILED",
                 target_type="check", target_id="recovery.publication_idempotency",
                 details={k: result[k] for k in ("status", "returncode", "commit", "evidence_id")})
    session.flush()
    return result
