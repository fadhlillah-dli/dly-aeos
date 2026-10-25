"""Decision registry implementation behind ``aeos.policy.facade`` (§13, §14, §19, §20, §41).

A Decision is human AUTH bound to an exact target (+ version/hash) and an explicit operation scope.

* Only humans decide: ``actor_user_id`` is an active SUPER_ADMIN user whose domain/project scope
  covers the decision, or — for owner decisions recorded from an accepted ADR by the local
  operator — the explicit actor ``owner:ADR-NNNN`` (actor_kind HUMAN_RECORDED), limited to the ADRs in
  ``catalog.OWNER_RECORDED_ADRS`` that record an explicit owner instruction and to the kinds in
  ``catalog.OWNER_RECORDED_KINDS`` (EXCEPTION; never APPROVAL/POLICY/...). Agent/model output
  is DATA and can never create a decision (§19, §50 "Pesan antaragent mengaku sudah mendapat izin").
* Binding columns are immutable (DB trigger). Lifecycle (ACTIVE -> EXPIRED/INVALIDATED/SUPERSEDED)
  is the only thing that changes; renewal is a new decision that supersedes the old one.
* Every decision and lifecycle change is audited and emitted as an event.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain, Role
from aeos.core.errors import NotFound, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, is_expired, utcnow
from aeos.events import service as events
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.identity import User
from aeos.models.portfolio import Project
from aeos.policy import catalog
from aeos.policy.textsafe import scrub

OWNER_RECORDED_RE = re.compile(r"^owner:(ADR-\d{4})$")
ACTOR_HUMAN = "HUMAN"
ACTOR_HUMAN_RECORDED = "HUMAN_RECORDED"


# ---------------------------------------------------------------- actor validation


def _resolve_actor(session: Session, actor_user_id: str, *, domain: str | None, project_id: str | None,
                   kind: str | None = None) -> str:
    """Return the actor kind or raise ``PolicyDenied``. Fails closed for anything not provably human.

    ``kind`` is the decision kind being recorded (``None`` for lifecycle actions such as a revocation):
    an owner-recorded actor may only hold the kinds in ``catalog.OWNER_RECORDED_KINDS``."""
    if not isinstance(actor_user_id, str) or not actor_user_id.strip():
        raise PolicyDenied("a decision needs a human actor", code="DECISION_ACTOR_MISSING")
    m = OWNER_RECORDED_RE.match(actor_user_id)
    if m:
        if kind not in catalog.OWNER_RECORDED_KINDS:
            # ADR-0003 §3: approvals, publication, risk, budget, policy and revocations stay with a live
            # SUPER_ADMIN session; an ADR-backed actor records only the delegated EXCEPTION.
            raise PolicyDenied(f"owner-recorded actors may only record {sorted(catalog.OWNER_RECORDED_KINDS)} "
                               f"decisions, not {kind or 'lifecycle changes'}", code="DECISION_ACTOR_NOT_AUTHORISED")
        if m.group(1) not in catalog.OWNER_RECORDED_ADRS:
            raise PolicyDenied(f"{m.group(1)} does not record an owner instruction; it cannot act as an owner",
                               code="DECISION_ACTOR_NOT_HUMAN")
        adr = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == m.group(1)))
        if adr is None or adr.status != "ACCEPTED":
            raise PolicyDenied(
                f"owner-recorded decisions must reference an ACCEPTED ADR ({m.group(1)} not loaded/accepted)",
                code="DECISION_ACTOR_NOT_HUMAN",
            )
        from aeos.policy import adrs as adr_register

        # §02: the owner's own ADR decision wins over the file's Status line - an ADR the owner REJECTED or
        # SUPERSEDED no longer backs an owner-recorded actor, even after the file is edited again
        owner_status = adr_register.effective_register_status(session, adr)
        if owner_status != "ACCEPTED":
            raise PolicyDenied(f"the owner recorded {owner_status} for {m.group(1)}; it no longer backs an "
                               "owner-recorded actor", code="DECISION_ACTOR_NOT_HUMAN")
        return ACTOR_HUMAN_RECORDED
    user = session.get(User, actor_user_id)
    if user is None or not user.is_active:
        raise PolicyDenied("decisions are human-only: actor is not an active user", code="DECISION_ACTOR_NOT_HUMAN")
    if user.role != Role.SUPER_ADMIN.value:
        raise PolicyDenied("only SUPER_ADMIN users hold decision AUTH", code="DECISION_ACTOR_NOT_AUTHORISED")
    if domain and user.domain_scope is not None and domain not in user.domain_scope:
        # §20: SUPER_ADMIN is not automatically AUTH over every domain/office resource.
        raise PolicyDenied("actor's domain scope does not include this decision", code="DECISION_SCOPE_DENIED")
    if project_id and user.project_scope is not None and project_id not in user.project_scope:
        raise PolicyDenied("actor's project scope does not include this decision", code="DECISION_SCOPE_DENIED")
    return ACTOR_HUMAN


def _clean_ops(ops: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for op in ops or []:
        if not isinstance(op, str) or not op.strip():
            raise ValidationFailed("allowed_operations must be non-empty strings")
        op = op.strip()
        if op not in out:
            out.append(op)
    return out


# ---------------------------------------------------------------- record


def record_decision(
    session: Session,
    *,
    kind: str,
    actor_user_id: str,
    target_type: str,
    target_id: str,
    target_version: str | None = None,
    target_hash: str | None = None,
    allowed_operations: list[str] | None = None,
    auth_scope: dict[str, Any] | None = None,
    project_id: str | None = None,
    domain: str | None = None,
    rationale: str = "",
    expires_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> Decision:
    if kind not in catalog.DECISION_KINDS:
        raise ValidationFailed(f"unknown decision kind {kind!r}", details={"allowed": sorted(catalog.DECISION_KINDS)})
    if not target_type or not target_id:
        raise ValidationFailed("a decision must name its exact target (target_type + target_id)")
    if len(target_type) > 64 or len(str(target_id)) > 128:
        raise ValidationFailed("target identifiers are too long")
    if domain is not None:
        try:
            domain = Domain(domain).value
        except ValueError as exc:
            raise ValidationFailed(f"invalid domain {domain!r}") from exc
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None:
            raise NotFound("project not found")
        if domain is None:
            domain = project.domain  # the project's immutable, human-chosen domain (never model output)
        elif project.domain != domain:
            raise PolicyDenied("decision domain does not match the project's domain", code="DOMAIN_MISMATCH")
    actor_kind = _resolve_actor(session, actor_user_id, domain=domain, project_id=project_id, kind=kind)
    ops = _clean_ops(allowed_operations)
    human_only = sorted(set(ops) & catalog.HUMAN_ONLY_OPERATIONS)
    if human_only and not (kind == "EXCEPTION" and actor_kind == ACTOR_HUMAN_RECORDED):
        # U-09: merge/production stay human actions; no AEOS decision can delegate them.
        raise PolicyDenied(f"human-only operations cannot be delegated: {human_only}", code="HUMAN_ONLY_OPERATION")
    expires_at = ensure_aware(expires_at)
    if expires_at is not None and expires_at <= utcnow():
        raise ValidationFailed("expires_at must be in the future")

    decision = Decision(
        kind=kind,
        project_id=project_id,
        domain=domain,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        auth_scope=scrub(auth_scope or {}),
        target_type=target_type,
        target_id=str(target_id),
        target_version=target_version,
        target_hash=target_hash,
        allowed_operations=ops,
        rationale=redact_text(rationale or ""),
        details=scrub(details or {}),
        expires_at=expires_at,
        status="ACTIVE",
    )
    session.add(decision)
    session.flush()
    audit.record(
        session, actor_type="user" if actor_kind == ACTOR_HUMAN else "owner-record", actor_id=actor_user_id,
        action="decision.record", target_type="decision", target_id=decision.id, project_id=project_id,
        domain=domain, reason=decision.rationale[:500],
        details={"kind": kind, "target_type": target_type, "target_id": str(target_id), "target_hash": target_hash,
                 "allowed_operations": ops, "expires_at": expires_at.isoformat() if expires_at else None},
    )
    events.emit(
        session, source="policy", kind="decision.recorded", entity_type="decision", entity_id=decision.id,
        entity_version=target_hash, project_id=project_id, domain=domain,
        payload={"kind": kind, "target_type": target_type, "target_id": str(target_id), "actor_kind": actor_kind},
    )
    if kind in catalog.SNAPSHOT_DECISION_KINDS:
        _refresh_snapshot(session, reason=f"decision {decision.id} recorded")
    _notify_readiness(session, [decision], reason=f"decision {decision.id} ({kind}) recorded")
    return decision


# ---------------------------------------------------------------- readiness invalidation (§08, §50)


def readiness_invalidators(decision: Decision) -> list[str]:
    """Doctor invalidators a decision lifecycle change affects (only the checks that depend on it)."""
    from aeos.policy.office import is_office_policy_decision

    out: list[str] = []
    if decision.kind in catalog.SNAPSHOT_DECISION_KINDS:
        out.append("config:policy")
    if is_office_policy_decision(decision):
        out.append("config:office_policy")
    if decision.kind == "WRITE_ENABLEMENT" or decision.target_type == catalog.TARGET_PROVIDER_WRITE:
        out.append("config:write_enablement")
    if decision.kind == "BUDGET":
        out.append("config:budget")
    if decision.kind in ("ROUTE_ACTIVATION", "TRIAL") or decision.target_type == "model_route":
        out += ["config:routes", "model:route"]
    if decision.target_type == "worker_sandbox":
        out.append("config:sandbox")
    if decision.target_type == "secret_store":
        out.append("secret:*")  # the selected secret backend may change (interim keyring exception)
    if decision.target_type == catalog.TARGET_CLAUDE_EXTRA_USAGE:
        out.append("config:claude_extra_usage")  # claude.extra_billing reads the owner attestation
    return list(dict.fromkeys(out))


def _route_pool_invalidator(session: Session, decision: Decision) -> str:
    """``model:route:<POOL>`` of the route a decision is about (the Claude bridge proof depends on SUPERVISOR
    routes only); unknown route -> ``model:route:*`` (conservative)."""
    if decision.target_type == "model_route" and decision.target_id:
        try:
            from aeos.models.routing import ModelRoute

            pool = session.scalar(select(ModelRoute.pool).where(ModelRoute.route_key == decision.target_id))
        except Exception:  # pragma: no cover - routing tables always exist with the policy module
            pool = None
        if pool:
            return f"model:route:{str(pool).upper()}"
    return "model:route:*"


def _notify_readiness(session: Session, decisions: list[Decision], *, reason: str) -> None:
    """Mark dependent Readiness Doctor checks STALE so capabilities stop being READY until a recheck.
    Same transaction as the decision change; a missing doctor module means nothing was fresh."""
    invalidators: list[str] = []
    for d in decisions:
        invs = readiness_invalidators(d)
        if "model:route" in invs:
            invs.append(_route_pool_invalidator(session, d))
        invalidators += invs
    invalidators = list(dict.fromkeys(invalidators))
    if not invalidators:
        return
    try:
        from aeos.doctor import facade as doctor
    except (ImportError, ModuleNotFoundError):
        return
    if any(d.domain == "OFFICE" for d in decisions):
        # Stored stale reasons and doctor.check.stale events of platform-level checks are visible to every
        # domain: an OFFICE decision id/kind is OFFICE metadata and is not quoted there (ADR-0011 #8).
        reason = "keputusan ber-scope domain berubah; detail di halaman Keputusan domain terkait"
    for inv in invalidators:
        doctor.invalidate(session, invalidator=inv, reason=redact_text(reason)[:200])


# ---------------------------------------------------------------- queries


def effective_status(decision: Decision) -> str:
    if decision.status == "ACTIVE" and is_expired(decision.expires_at):
        return "EXPIRED"
    return decision.status


def is_active(decision: Decision | None) -> bool:
    return decision is not None and effective_status(decision) == "ACTIVE"


def find_active_decision(
    session: Session,
    *,
    kind: str,
    target_type: str,
    target_id: str,
    target_hash: str | None = None,
    operation: str | None = None,
) -> Decision | None:
    """Newest ACTIVE, unexpired decision of *kind* for the exact target. A requested hash must match
    the bound hash exactly (a decision without a hash never matches a hash-bound request)."""
    rows = session.scalars(
        select(Decision)
        .where(Decision.kind == kind, Decision.target_type == target_type, Decision.target_id == str(target_id),
               Decision.status == "ACTIVE")
        .order_by(Decision.created_at.desc())
    )
    for d in rows:
        if is_expired(d.expires_at):
            continue
        if target_hash is not None and d.target_hash != target_hash:
            continue
        if operation is not None and operation not in (d.allowed_operations or []):
            continue
        return d
    return None


def active_decisions(session: Session, *, kinds: Iterable[str], domain: str | None = None) -> list[Decision]:
    q = select(Decision).where(Decision.kind.in_(list(kinds)), Decision.status == "ACTIVE")
    if domain is not None:
        q = q.where(Decision.domain == domain)
    return [d for d in session.scalars(q.order_by(Decision.created_at.asc())) if not is_expired(d.expires_at)]


# ---------------------------------------------------------------- lifecycle


def _set_lifecycle(session: Session, d: Decision, *, status: str, reason: str, actor_type: str, actor_id: str,
                   superseded_by: str | None = None) -> None:
    previous = d.status
    d.status = status
    d.invalidated_at = utcnow()
    d.invalidation_reason = redact_text(reason)[:2000]
    if superseded_by:
        d.superseded_by = superseded_by
    session.flush()
    kind_event = {"INVALIDATED": "decision.invalidated", "EXPIRED": "decision.expired",
                  "SUPERSEDED": "decision.superseded"}[status]
    audit.record(
        session, actor_type=actor_type, actor_id=actor_id, action=kind_event, target_type="decision",
        target_id=d.id, project_id=d.project_id, domain=d.domain, reason=d.invalidation_reason or "",
        details={"previous_status": previous, "kind": d.kind, "target_type": d.target_type, "target_id": d.target_id,
                 "superseded_by": superseded_by},
    )
    events.emit(
        session, source="policy", kind=kind_event, entity_type="decision", entity_id=d.id,
        entity_version=d.target_hash, project_id=d.project_id, domain=d.domain,
        payload={"kind": d.kind, "target_type": d.target_type, "target_id": d.target_id, "reason": d.invalidation_reason},
    )


def _after_lifecycle(session: Session, changed: list[Decision], *, plan_hook: bool = True) -> None:
    """Keep dependent state consistent when decisions stop being ACTIVE (fail closed)."""
    if not changed:
        return
    from aeos.policy import enablement

    for d in changed:
        if (d.kind == "WRITE_ENABLEMENT" and d.target_type == catalog.TARGET_PROVIDER_WRITE
                and enablement.active_enablement_decision(session, d.target_id) is None):
            enablement.set_flag(session, d.target_id, False)
    if any(d.kind in catalog.SNAPSHOT_DECISION_KINDS for d in changed):
        _refresh_snapshot(session, reason="decision lifecycle change")
    _notify_readiness(session, changed, reason="decision lifecycle change: " + ", ".join(
        f"{d.id[:8]} {d.kind} -> {d.status}" for d in changed[:5]))
    if plan_hook:
        approvals = [d for d in changed if d.kind == "APPROVAL" and d.target_type == catalog.TARGET_PLAN_VERSION]
        if approvals:
            from aeos.planning import service as planning

            for d in approvals:
                planning.on_approval_inactive(session, d)


def invalidate_decision(
    session: Session, decision: Decision, *, reason: str, actor_type: str = "system", actor_id: str = "policy",
    plan_hook: bool = True,
) -> bool:
    if decision.status != "ACTIVE":
        return False
    _set_lifecycle(session, decision, status="INVALIDATED", reason=reason, actor_type=actor_type, actor_id=actor_id)
    _after_lifecycle(session, [decision], plan_hook=plan_hook)
    return True


def invalidate_decisions(
    session: Session, *, target_type: str, target_id: str, reason: str, kinds: Iterable[str] | None = None,
    actor_type: str = "system", actor_id: str = "policy", plan_hook: bool = True,
) -> int:
    q = select(Decision).where(Decision.target_type == target_type, Decision.target_id == str(target_id),
                               Decision.status == "ACTIVE")
    if kinds is not None:
        q = q.where(Decision.kind.in_(list(kinds)))
    changed = list(session.scalars(q))
    for d in changed:
        _set_lifecycle(session, d, status="INVALIDATED", reason=reason, actor_type=actor_type, actor_id=actor_id)
    _after_lifecycle(session, changed, plan_hook=plan_hook)
    return len(changed)


def supersede_decision(session: Session, old: Decision, *, new_decision_id: str, reason: str,
                       actor_type: str = "system", actor_id: str = "policy", plan_hook: bool = True) -> bool:
    if old.status != "ACTIVE":
        return False
    _set_lifecycle(session, old, status="SUPERSEDED", reason=reason, actor_type=actor_type, actor_id=actor_id,
                   superseded_by=new_decision_id)
    _after_lifecycle(session, [old], plan_hook=plan_hook)
    return True


def expire_due_decisions(session: Session) -> int:
    """Persist EXPIRED for ACTIVE decisions past ``expires_at`` (idempotent reconciliation)."""
    now = utcnow()
    due = [d for d in session.scalars(select(Decision).where(Decision.status == "ACTIVE", Decision.expires_at.is_not(None)))
           if (exp := ensure_aware(d.expires_at)) is not None and exp <= now]
    for d in due:
        _set_lifecycle(session, d, status="EXPIRED", reason="expires_at reached", actor_type="system", actor_id="policy")
    _after_lifecycle(session, due)
    return len(due)


def revoke_decision(session: Session, decision_id: str, *, actor_user_id: str, reason: str) -> Decision:
    d = session.get(Decision, decision_id)
    if d is None:
        raise NotFound("decision not found")
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("a revocation needs a rationale")
    _resolve_actor(session, actor_user_id, domain=d.domain, project_id=d.project_id)
    if d.status != "ACTIVE":
        raise ValidationFailed(f"decision is already {d.status}", code="DECISION_NOT_ACTIVE")
    invalidate_decision(session, d, reason=f"revoked: {reason.strip()}", actor_type="user", actor_id=actor_user_id)
    return d


# ---------------------------------------------------------------- views


def _actor_display(session: Session, d: Decision) -> str:
    if d.actor_kind == ACTOR_HUMAN_RECORDED:
        return d.actor_user_id
    user = session.get(User, d.actor_user_id)
    return user.username if user else d.actor_user_id


def decision_view(session: Session, d: Decision) -> dict[str, Any]:
    return {
        "id": d.id,
        "kind": d.kind,
        "status": effective_status(d),
        "stored_status": d.status,
        "project_id": d.project_id,
        "domain": d.domain,
        "actor_user_id": d.actor_user_id,
        "actor": _actor_display(session, d),
        "actor_kind": d.actor_kind,
        "auth_scope": d.auth_scope or {},
        "target_type": d.target_type,
        "target_id": d.target_id,
        "target_version": d.target_version,
        "target_hash": d.target_hash,
        "allowed_operations": d.allowed_operations or [],
        "rationale": d.rationale,
        "details": d.details or {},
        "expires_at": d.expires_at.isoformat() if d.expires_at else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "invalidated_at": d.invalidated_at.isoformat() if d.invalidated_at else None,
        "invalidation_reason": d.invalidation_reason,
        "superseded_by": d.superseded_by,
    }


def _refresh_snapshot(session: Session, *, reason: str) -> None:
    from aeos.policy import snapshot

    snapshot.ensure_active_snapshot(session, source=reason)
