"""Cross-module contract for task execution state (§14). Owned by ``aeos.workforce``.

Only the scheduler mutates TaskExecution/WorkerRun state; other modules request transitions here.
Exit code 0 from a worker only moves a run to REVIEW; DONE requires a valid AcceptanceRecord.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.workforce import TaskExecution, WorkerRun


def transition_task(
    session: Session, task_id: str, to_state: str, *, reason: str, actor: str, expected_from: str | None = None,
    reason_code: str = "",
) -> TaskExecution:
    """Apply a legal §14 transition with optimistic concurrency; raise ``Conflict`` otherwise.
    ``reason_code`` (additive, integration 2026-10-18) lets the caller name why, e.g. ``QUARANTINED`` or
    ``REVIEW_BLOCKED`` — BLOCKED tasks with those codes wait for an owner action instead of being retried."""
    from aeos.workforce.scheduler import transition_task as impl

    return impl(session, task_id, to_state, reason=reason, actor=actor, expected_from=expected_from,
                reason_code=reason_code)


def update_run(session: Session, run_id: str, *, actor: str, **fields: Any) -> WorkerRun:
    """Update observed run fields (verifier_status, review_verdict, acceptance_status, ...)."""
    from aeos.workforce.scheduler import update_run as impl

    return impl(session, run_id, actor=actor, **fields)


def request_repair(session: Session, task_id: str, *, findings: list[Any], review_record_id: str | None, actor: str) -> TaskExecution:
    """Reviewer findings → new bounded attempt (READY) or BLOCKED/NEEDS_DECISION when attempts are used up."""
    from aeos.workforce.scheduler import request_repair as impl

    return impl(session, task_id, findings=findings, review_record_id=review_record_id, actor=actor)


def mark_accepted(session: Session, task_id: str, run_id: str, *, acceptance_id: str, actor: str) -> TaskExecution:
    from aeos.workforce.scheduler import mark_accepted as impl

    return impl(session, task_id, run_id, acceptance_id=acceptance_id, actor=actor)


def reopen_for_reverification(session: Session, task_id: str, *, reason: str, actor: str) -> TaskExecution:
    """DONE -> READY (new bounded attempt) or BLOCKED (no attempt left / plan no longer approved) when the
    acceptance the task rests on became STALE (head/base/config/plan changed, §14, §50). Idempotent for
    tasks that are not DONE. Added by the integration of seam group W2 (2026-10-18)."""
    from aeos.workforce.scheduler import reopen_for_reverification as impl

    return impl(session, task_id, reason=reason, actor=actor)


def cancel_run(session: Session, run_id: str, *, reason: str, actor: str) -> WorkerRun:
    """Revoke capabilities, kill the process tree/sandbox, release reservations, keep evidence."""
    from aeos.workforce.scheduler import cancel_run as impl

    return impl(session, run_id, reason=reason, actor=actor)


def set_scheduling_paused(session: Session, paused: bool, *, actor: str, reason: str = "") -> None:
    from aeos.workforce.scheduler import set_scheduling_paused as impl

    impl(session, paused, actor=actor, reason=reason)


def scheduling_paused(session: Session) -> bool:
    from aeos.workforce.scheduler import scheduling_paused as impl

    return impl(session)


def pause_info(session: Session) -> dict[str, Any]:
    """The scheduler pause record ({paused, reason, by, at}); ``by`` tells who set the current pause."""
    from aeos.workforce.scheduler import pause_info as impl

    return impl(session)
