"""Neutralise foreign trust-table rows found by the verifier's tamper window (§21; review W6-057/W6-056/W6-060).

Detection alone left a forged row in effect after the run was QUARANTINED: an ACTIVE ``Decision`` injected by
verifier-executed candidate code was still honoured by ``find_active_decision``, a forged session still logged in, a
tampered route still routed. ``neutralize_foreign_rows`` takes every *added* or *modified* foreign row the window
reported and makes it ineffective - fail closed, never by trusting the row's content:

=========================  ==========================================================================================
``decisions``              ACTIVE -> INVALIDATED through ``aeos.policy.decisions`` (audit/event, readiness, plan hooks)
``decision_requests``      OPEN -> WITHDRAWN (a human decides nothing a forged request asks)
``acceptance_records``     the task's ACCEPTED/PENDING acceptance -> INVALIDATED (re-verification required)
``auth_sessions``          revoked (``revoked_reason = verifier-tamper``)
``capability_grants``      revoked and inactive
``users``                  deactivated, sessions revoked (a modified existing account too - its password hash or role
                           may be the candidate's; ``aeos admin recover`` re-activates it locally); a modified one is
                           also listed for the owner's role/scope review
``model_routes``           DISABLED (ineligible until a human re-activates its configuration hash); its route
                           decisions invalidated, readiness checks marked STALE
``policy_snapshots``       deactivated; the active snapshot rebuilt from the policy files + decisions
``system_settings``        a containment proof removed (re-run ``aeos worker containment``); write enablement off
=========================  ==========================================================================================

Everything else (removed rows, TaskSpecs/PlanVersions/evidence, budgets, agents, projects, the schema, ...) has no
safe automatic inverse and is listed under ``manual`` with the owner's remedy. A foreign row may also be the owner's
own write from another AEOS process during the window (stated in ``aeos.verification.tamper``): it is neutralised
all the same (fail closed) and the owner re-records it. The caller commits.

Controller checkout/interpreter files are never restored automatically (``controller_remedies`` lists the remedy):
whatever stays in effect - such a file, a ``manual`` row, a failed neutralisation - makes the verifier pause
admission of new worker runs (integrity hold) until the owner reconciles and resumes it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.timeutil import utcnow

log = logging.getLogger("aeos.verification.neutralize")

ACTOR = "verifier"
REVOKED_REASON = "verifier-tamper"
ROUTE_TARGET_TYPE = "model_route"  # aeos.routing.service.TARGET_TYPE (Decision.target_type of route activations)
CONTAINMENT_PREFIX = "worker_runtime.containment."
WRITE_ENABLED_PREFIX = "write_enabled."
MAX_LISTED = 200  # entries per list kept in the stored report (the counts are always complete)


def _reason(run_id: str, table: str, change: str) -> str:
    return (f"VERIFIER_TAMPER: {change} by verifier-executed candidate code of run {run_id} ({table}); neutralised "
            "until the owner's special review")


def _decision(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.decisions import Decision
    from aeos.policy import decisions as registry

    d = session.get(Decision, row_id, populate_existing=True)
    if d is None:
        return "GONE"
    if d.status != "ACTIVE":
        return f"ALREADY_{d.status}"
    registry.invalidate_decision(session, d, reason=reason, actor_type="system", actor_id=ACTOR)
    return "INVALIDATED"


def _decision_request(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.policy_ext import DecisionRequest

    r = session.get(DecisionRequest, row_id, populate_existing=True)
    if r is None:
        return "GONE"
    if r.status != "OPEN":
        return "MANUAL"
    r.status = "WITHDRAWN"
    r.resolved_at = utcnow()
    r.resolved_by = ACTOR
    r.resolution_note = reason[:2000]
    return "WITHDRAWN"


def _acceptance(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.evidence import AcceptanceRecord
    from aeos.verification import acceptance

    rec = session.get(AcceptanceRecord, row_id, populate_existing=True)
    if rec is None:
        return "GONE"
    if rec.acceptance_status not in (acceptance.ACCEPTED, "PENDING"):
        return f"ALREADY_{rec.acceptance_status}"
    acceptance.invalidate(session, task_id=rec.task_id, reason=reason, actor=ACTOR, to_status=acceptance.INVALIDATED)
    return "INVALIDATED"


def _auth_session(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.identity import AuthSession

    s = session.get(AuthSession, row_id, populate_existing=True)
    if s is None:
        return "GONE"
    if s.revoked_at is not None:
        return "ALREADY_REVOKED"
    s.revoked_at = utcnow()
    s.revoked_reason = REVOKED_REASON
    return "REVOKED"


def _grant(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.routing import CapabilityGrant

    g = session.get(CapabilityGrant, row_id, populate_existing=True)
    if g is None:
        return "GONE"
    if g.revoked_at is not None and not g.active:
        return "ALREADY_REVOKED"
    g.active = False
    g.revoked_at = g.revoked_at or utcnow()
    g.revoked_reason = REVOKED_REASON
    return "REVOKED"


def _user(session: Session, row_id: str, reason: str, change: str) -> str:
    from aeos.identity import service as identity
    from aeos.models.identity import User

    u = session.get(User, row_id, populate_existing=True)
    if u is None:
        return "GONE"
    identity.revoke_all_sessions(session, u.id, reason=REVOKED_REASON)
    # An added account is forged. A modified existing one (possibly the owner's own) may carry a password hash or
    # role the candidate chose: it must not sign in on the web or prove a CLI decision (``--as``, which checks the
    # password and ``is_active`` only) with it. Deactivated, it is recovered locally by the owner - ``aeos admin
    # recover <username>`` re-activates it with a one-time password (no lock-out: the local operator always can).
    u.is_active = False
    return "DEACTIVATED"


def _route(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.routing import ModelRoute
    from aeos.policy import decisions as registry

    r = session.get(ModelRoute, row_id, populate_existing=True)
    if r is None:
        return "GONE"
    previous = r.status
    r.status = "DISABLED"
    registry.invalidate_decisions(session, target_type=ROUTE_TARGET_TYPE, target_id=r.route_key, reason=reason,
                                  actor_type="system", actor_id=ACTOR)
    try:
        from aeos.doctor import facade as doctor

        for inv in ("config:routes", "model:route", f"model:route:{(r.pool or '*').upper()}"):
            doctor.invalidate(session, invalidator=inv, reason=f"{r.route_key}: {previous} -> DISABLED (tamper)"[:200])
    except Exception:  # readiness is re-derived from the DISABLED status anyway
        log.info("doctor invalidation after route neutralisation failed", exc_info=True)
    return "DISABLED" if previous != "DISABLED" else "ALREADY_DISABLED"


def _policy_snapshot(session: Session, row_id: str, reason: str) -> str:
    from aeos.models.decisions import PolicySnapshot

    s = session.get(PolicySnapshot, row_id, populate_existing=True)
    if s is None:
        return "GONE"
    was_active = bool(s.active)
    s.active = False
    session.flush()
    return "DEACTIVATED" if was_active else "INACTIVE"


def _setting(session: Session, key: str, reason: str) -> str:
    from aeos.models.identity import SystemSetting

    row = session.get(SystemSetting, key, populate_existing=True)
    if row is None:
        return "GONE"
    if key.startswith(CONTAINMENT_PREFIX):
        session.delete(row)  # the isolation proof must be re-run by the owner, never inherited from a forged row
        return "REMOVED"
    if key.startswith(WRITE_ENABLED_PREFIX):
        if row.value is not False:
            row.value = False
            row.version = (row.version or 1) + 1
        return "DISABLED"
    return "MANUAL"


_MANUAL_REMEDY = {
    "removed": "row removed by candidate code: re-record it, or restore the newest backup taken before the run if "
               "one exists (aeos ops backup list, aeos ops backup restore - a whole-database restore: review what "
               "changed since) after review",
    "sqlite_schema": "schema/trigger changed: restore the newest backup taken before the run if one exists (aeos ops "
                     "backup list, aeos ops backup restore) after review; aeos db status shows the migration head",
    "users": "an existing account was modified: it is deactivated and its sessions revoked - recover it locally "
             "(aeos admin recover <username>: re-activates it with a one-time password), then check its role and "
             "project/domain scope",
    "decision_requests": "a resolved request was modified: compare it with the decision it names",
    "system_settings": "a feature/bootstrap setting was changed: compare it with the last known value",
}


INTERPRETER_PREFIX = "interpreter:"  # aeos.worker_runtime.integrity.INTERPRETER_PREFIX


def controller_remedies(paths: list[str]) -> list[dict[str, str]]:
    """The owner's remedy for each controller checkout/interpreter path candidate code changed. Nothing is restored
    automatically: the checkout may also hold the owner's own uncommitted work, and an interpreter file has no
    reviewed source to restore from. Admission stays paused (integrity hold) until the owner resumes it."""
    out: list[dict[str, str]] = []
    for path in list(dict.fromkeys(paths))[:20]:
        if path == "*":
            remedy = ("the controller integrity snapshot could not be taken or compared: check the controller checkout "
                      "(git status, git diff) and the interpreter before resuming")
        elif path.startswith(INTERPRETER_PREFIX):
            remedy = ("controller interpreter file changed: stop AEOS, remove the .venv directory and recreate it with "
                      "the pinned dependencies (scripts/bootstrap.ps1 on Windows, scripts/bootstrap.sh on macOS) before "
                      "resuming; do not keep the changed file")
        elif path.startswith("policy/"):
            remedy = (f"policy file changed: compare it with git (git diff -- {path}), restore it if the change is not "
                      "yours (git checkout -- <path>), then rebuild the active policy snapshot "
                      "(aeos policy snapshot --refresh)")
        else:
            remedy = (f"controller file changed: compare it with git (git diff -- {path}), restore it if the change is "
                      "not yours (git checkout -- <path>), then restart AEOS so no changed code stays loaded")
        out.append({"path": path, "remedy": remedy})
    return out


def neutralize_foreign_rows(session: Session, foreign: list[dict[str, Any]], *, run_id: str,
                            project_id: str | None = None, domain: str | None = None,
                            foreign_count: int | None = None) -> dict[str, Any]:
    """Neutralise the foreign rows of one tamper window (see the module docstring) - pass every row with its full
    key (``tamper.Window.foreign_all``), not only the report's first rows. Returns ``{"actions": [...], "manual":
    [...], "failed": [...], "not_listed": n, "counts": {...}}`` and records one audit event."""
    from aeos.verification import tamper

    actions: list[dict[str, str]] = []
    manual: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    snapshot_touched = False
    # every row is re-read from the database (``populate_existing``): the controller session may hold a stale copy
    # of a row the candidate rewrote behind its back; the controller's own pending changes are flushed first
    session.flush()
    for f in foreign:
        table, row_id, change = str(f.get("table") or ""), str(f.get("id") or ""), str(f.get("change") or "")
        entry = {"table": table, "id": row_id[:64], "change": change}  # looked up by the full key, listed shortened
        if change == "removed" or table == tamper.SCHEMA_KEY:
            manual.append({**entry, "remedy": _MANUAL_REMEDY["removed" if change == "removed" else table]})
            continue
        reason = _reason(run_id, table, f"foreign row {change}")
        try:
            with session.begin_nested():
                if table == "decisions":
                    result = _decision(session, row_id, reason)
                elif table == "decision_requests":
                    result = _decision_request(session, row_id, reason)
                elif table == "acceptance_records":
                    result = _acceptance(session, row_id, reason)
                elif table == "auth_sessions":
                    result = _auth_session(session, row_id, reason)
                elif table == "capability_grants":
                    result = _grant(session, row_id, reason)
                elif table == "users":
                    result = _user(session, row_id, reason, change)
                elif table == "model_routes":
                    result = _route(session, row_id, reason)
                elif table == "policy_snapshots":
                    result = _policy_snapshot(session, row_id, reason)
                    snapshot_touched = True
                elif table == "system_settings":
                    result = _setting(session, row_id, reason)
                else:
                    result = "MANUAL"
                session.flush()
        except Exception as exc:  # one row never stops the others; the failure is reported, not hidden
            failed.append({**entry, "error": type(exc).__name__})
            continue
        if result == "MANUAL":
            manual.append({**entry, "remedy": _MANUAL_REMEDY.get(
                table, "no safe automatic inverse: review the row against the owner's records and correct it")})
        elif result == "GONE":
            # an added/modified row that cannot be found again is not proven ineffective (fail closed)
            manual.append({**entry, "remedy": "the foreign row was not found again by its key when neutralising: "
                                              "check the table for it before resuming"})
        else:
            actions.append({**entry, "action": result})
            if table == "users" and change != "added":
                # the owner's own account, perhaps: deactivated (above), and its role/scope are the owner's review
                manual.append({**entry, "remedy": _MANUAL_REMEDY["users"]})
    if snapshot_touched:
        try:
            from aeos.policy import decisions as registry

            with session.begin_nested():
                registry._refresh_snapshot(session, reason="verifier tamper: foreign policy snapshot neutralised")
            actions.append({"table": "policy_snapshots", "id": "*", "change": "rebuilt", "action": "ACTIVE_REBUILT"})
        except Exception as exc:
            failed.append({"table": "policy_snapshots", "id": "*", "change": "rebuild", "error": type(exc).__name__})
    listed = len(foreign)
    not_listed = max(0, int(foreign_count or listed) - listed)
    if not_listed:  # first, so it is never cut from the stored list below
        manual.insert(0, {"table": "*", "id": "*", "change": f"{not_listed} more foreign row(s)",
                          "remedy": "only the first rows of the window are listed (the rest is NOT neutralised): "
                                    "compare the trust tables with the newest backup taken before the run, if one "
                                    "exists"})
    # the stored report keeps the first entries of each list and the full counts (thousands of forged rows are
    # all neutralised above, but never all quoted)
    result_doc = {"actions": actions[:MAX_LISTED], "manual": manual[:MAX_LISTED], "failed": failed[:MAX_LISTED],
                  "not_listed": not_listed,
                  "counts": {"actions": len(actions), "manual": len(manual), "failed": len(failed)}}
    audit.record(session, actor_type="system", actor_id=ACTOR, action="verification.tamper.neutralize",
                 result="FAILED" if failed else "SUCCESS", target_type="worker_run", target_id=run_id,
                 project_id=project_id, domain=domain,
                 reason=f"{len(actions)} foreign row(s) neutralised, {len(manual)} for manual review"[:500],
                 details={"actions": actions[:50], "manual": manual[:50], "failed": failed[:20]})
    return result_doc
