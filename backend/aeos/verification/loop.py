"""Deterministic verifier trigger (§16 "Evidence Collector" / §19 independent verifier as a runner).

When a WorkerRun reaches REVIEW with a candidate workspace and a head commit, and no verification
report exists yet for that exact head, the verifier runs automatically — one run at a time, never
for the same head twice (a BLOCKED result is not retried automatically; the operator re-runs it).
No LLM is involved. Exposes ``start(settings)`` / ``stop()`` for the app lifespan (registration in
``aeos.api.routers.LIFECYCLE_HOOKS`` is owned by the integrator) and ``run_once()`` for the CLI.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import select

from aeos.core.config import Settings
from aeos.core.db import session_factory
from aeos.core.enums import ExecutionState
from aeos.core.errors import AeosError
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification.verifier import latest_report, report_view, verify_run

log = logging.getLogger("aeos.verification.loop")
POLL_SECONDS = 30.0
ACTOR = "verifier-loop"

_stop = threading.Event()
_thread: threading.Thread | None = None


def pending_runs(session: Any) -> list[WorkerRun]:
    """REVIEW runs with a recorded head and no report for it yet, of the platform project or of a
    project whose owner registered a trusted suite (integration amendment, ADR-0060)."""
    from aeos.verification import suite

    rows = session.execute(select(WorkerRun, Project).join(Project, Project.id == WorkerRun.project_id).where(
        WorkerRun.execution_status == ExecutionState.REVIEW.value, WorkerRun.workspace_ref.is_not(None),
        WorkerRun.head_commit.is_not(None), WorkerRun.mode == "LIVE",
    ).order_by(WorkerRun.updated_at.asc())).all()
    out: list[WorkerRun] = []
    for run, project in rows:
        if not project.is_platform and not suite.has_registration(session, project.id):
            continue
        view = report_view(latest_report(session, run.id))
        # A report counts for this head when it resolved it (head_commit) or was requested for it
        # (requested_head — also recorded when the run was BLOCKED before the head could be resolved),
        # so a BLOCKED verification is never retried automatically for the same head.
        if view is None or run.head_commit not in (view.get("head_commit"), view.get("requested_head")):
            out.append(run)
    return out


def run_once(*, limit: int = 1) -> list[dict[str, Any]]:
    """Verify up to *limit* pending runs; each in its own transaction."""
    results: list[dict[str, Any]] = []
    with session_factory()() as s:
        ids = [r.id for r in pending_runs(s)[:limit]]
    for run_id in ids:
        with session_factory()() as s:
            try:
                out = verify_run(s, run_id, actor=ACTOR, actor_type="system")
                s.commit()
                results.append({"run_id": run_id, "status": out.status, "report": out.report_evidence_id})
            except AeosError as exc:
                s.rollback()
                results.append({"run_id": run_id, "status": "SKIPPED", "reason": f"{exc.code}: {exc.message}"})
    return results


def recheck_accepted(*, limit: int = 50) -> list[dict[str, Any]]:
    """Persist drift of ACCEPTED LIVE records (run/PR head, PR base, grader/verifier configuration,
    plan hash): ``recheck`` marks them STALE and the scheduler reopens a DONE task for re-verification
    (§14 "... membatalkan acceptance/approval lama dan memicu verifikasi ulang"). Read-only otherwise."""
    from aeos.models.evidence import AcceptanceRecord
    from aeos.verification import acceptance

    out: list[dict[str, Any]] = []
    with session_factory()() as s:
        ids = list(s.scalars(select(AcceptanceRecord.id).where(
            AcceptanceRecord.acceptance_status == acceptance.ACCEPTED, AcceptanceRecord.mode == "LIVE")
            .order_by(AcceptanceRecord.updated_at.asc()).limit(limit)))
    for acc_id in ids:
        with session_factory()() as s:
            try:
                rec = s.get(AcceptanceRecord, acc_id)
                stale = acceptance.staleness(s, rec) if rec is not None else []
                if not stale or all("UNKNOWN" in reason for reason in stale):
                    # nothing drifted, or only a transiently unreadable configuration: the record is
                    # already invalid on read (fail closed); nothing is reopened on an UNKNOWN alone
                    continue
                rec = acceptance.recheck(s, acc_id, actor=ACTOR)
                s.commit()
                if rec.acceptance_status != acceptance.ACCEPTED:
                    out.append({"acceptance_id": acc_id, "status": rec.acceptance_status,
                                "reason": rec.invalidated_reason})
            except AeosError as exc:
                s.rollback()
                out.append({"acceptance_id": acc_id, "status": "SKIPPED", "reason": f"{exc.code}: {exc.message}"})
    return out


def _loop() -> None:
    while not _stop.wait(POLL_SECONDS):
        try:
            for r in run_once():
                log.info("verifier loop: run %s -> %s", r["run_id"], r["status"])
            for r in recheck_accepted():
                log.info("verifier loop: acceptance %s -> %s", r["acceptance_id"], r["status"])
        except Exception:  # pragma: no cover - logged; the next tick retries, nothing is assumed passed
            log.exception("verifier loop tick failed")


def start(settings: Settings) -> None:
    """Lifecycle hook (settings unused: the engine was initialised by the app lifespan)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="aeos-verifier-loop", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
    _thread = None
