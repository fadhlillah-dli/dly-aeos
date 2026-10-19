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


# ---- additive (cluster C7, 2026-10-19): RoleSkillPack contract for the task compiler and runs ----------------


def executor_role_keys() -> frozenset[str]:
    """Role keys a TaskSpec may name (executor RoleSkillPacks of the §16 catalog)."""
    from aeos.workforce.roles import executor_role_keys as impl

    return impl()


def role_revision(session: Session | None, role_key: str) -> str | None:
    """Revision hash of an executor RoleSkillPack (None when *role_key* is not a catalog executor)."""
    from aeos.workforce.roles import role_revision as impl

    return impl(session, role_key)


def role_contract(session: Session | None, role_key: str | None) -> dict[str, Any] | None:
    """Competence contract of the role (inputs, outputs, boundaries, forbidden actions, verification,
    failure modes, allowed scope, revision) — what the worker prompt and the checkpoint package carry."""
    from aeos.workforce.roles import role_contract as impl

    return impl(session, role_key)


def refresh_role_eval_status(session: Session, role_key: str | None = None) -> int:
    """Re-derive RoleSkillPack.eval_status from EvalRuns bound to the current revision (§46)."""
    from aeos.workforce.roles import refresh_role_eval_status as impl

    return impl(session, role_key)
def pause_info(session: Session) -> dict[str, Any]:
    """The scheduler pause record ({paused, reason, by, at}); ``by`` tells who set the current pause."""
    from aeos.workforce.scheduler import pause_info as impl

    return impl(session)
