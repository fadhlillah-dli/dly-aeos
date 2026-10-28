"""Owner-only change of a project's data classification (ADR-0133; master §04, §20, §23, §24).

A project's class is the floor of what is planned for it (``planning.dag.task_classification``: a task with no stated
class gets the project's class, a stated class can only raise it, an unknown label becomes RESTRICTED), of its
attachments and memory; routing applies the higher of a task's class and its project's current class
(``routing.task_class``), and the delivery gate G0 refuses a task below it. It is chosen at creation; this module is
the one audited path that changes it afterwards.

* **Raising** (more restrictive) needs no decision: an actor who may write (an active SUPER_ADMIN whose scope covers
  the project; VIEWER is read-only, §02) gets one audit record and one event. A raise also ends (INVALIDATED) the
  project's ACTIVE lowering decision, which no longer describes the class.
* **Lowering** is a human POLICY decision on ``project_classification/<project id>`` bound to the old and new class
  (``target_version`` = new class, ``target_hash`` over project/from/to), recorded with an audit record and an event;
  all three name the old class, the new class, the reason and the actor. The actor must be an active SUPER_ADMIN
  (the decision registry re-checks it and its scope). The decision is written by ``aeos.policy.reclassification``
  through ``aeos.policy.facade``. A newer lowering supersedes the previous decision; revoking
  the newest one (``aeos policy revoke``) restores the class it lowered from (``on_decision_inactive``).
* **OFFICE is never lowered here**: which OFFICE data classes may leave the host is the company's authorisation
  (§23, ``approved_data_classes``), not an owner choice. Raising an OFFICE project stays allowed.
* **In-flight work is never downgraded.** TaskSpecs are immutable (DB trigger) and keep the class they were compiled
  with, so a lowering applies only to plans and task DAGs compiled afterwards. A raise applies to open work at once:
  routing uses the higher of the task's class and the project's current class.
* The HTTP ``PATCH /api/projects/{id}`` never changes the class (``service.update_project`` refuses it); the CLI
  ``aeos project reclassify`` is the owner path (the owner approves, the maintainer runs it as the owner's actor and
  the records name that actor and the reason).

Never commits the change: the caller commits. A refusal is committed here as a DENIED audit record (fail closed,
durable), after the unit of work was rolled back.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import TERMINAL_EXECUTION_STATES, Domain, Role
from aeos.core.errors import AeosError, NotFound, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware
from aeos.events import service as events
from aeos.models.decisions import Decision
from aeos.models.identity import User
from aeos.models.portfolio import Project
from aeos.policy import facade as policy
from aeos.portfolio import classification as cls
from aeos.portfolio.service import MAX_DESCRIPTION, clean_text, require_writable

ACTION = "portfolio.project.reclassify"
EVENT = "portfolio.project.reclassified"
TARGET = policy.PROJECT_CLASSIFICATION_TARGET
MIN_REASON = 10
MAX_APPROVAL_REF = 300
# ADR-0133 decision 8: why a lowering may be recorded without a password, stated in the decision itself
ACTOR_CHECK = ("named active SUPER_ADMIN, no password prompt (CLI): allowed only because the owner approves in chat "
               "and the maintainer runs it as the owner's actor (owner rule R43); approval_ref names that approval")
LOWER, RAISE, UNCHANGED = "LOWER", "RAISE", "UNCHANGED"
CLASSES = tuple(c.value for c in cls.ORDER)
STRICTEST = CLASSES[-1]
IN_FLIGHT_RULE = ("existing TaskSpecs keep the class they were compiled with (no silent downgrade of in-flight work); "
                  "routing applies the higher of a task's class and its project's current class, so a raise applies "
                  "to open tasks at once; plans and task DAGs compiled after this change use the new class as their "
                  "floor")
OFFICE_REFUSAL = ("an OFFICE project's data classification is never lowered by this command: which OFFICE data classes "
                  "may leave this host is the company's authorisation (master s23, approved_data_classes), not an "
                  "owner choice; raising it stays allowed")


def _deny(session: Session, *, actor_id: str, project: Project, code: str, reason: str,
          details: dict[str, Any] | None = None, exc_cls: type[AeosError] = PolicyDenied) -> NoReturn:
    """Roll back the unit of work, record the refusal durably, then raise (fail closed)."""
    pid, pdomain = project.id, project.domain
    session.rollback()
    audit.record(session, actor_type="user", actor_id=actor_id or "-", action=ACTION, result="DENIED",
                 target_type="project", target_id=pid, project_id=pid, domain=pdomain, reason=reason,
                 details={"code": code, **(details or {})})
    session.commit()
    raise exc_cls(reason, code=code, details=details or {})


def _domain(value: Any) -> Domain:
    text = str(value.value if isinstance(value, Domain) else value or "").strip().upper()
    if text not in {d.value for d in Domain}:
        raise ValidationFailed("domain must be exactly PERSONAL or OFFICE (it is never inferred)",
                               code="DOMAIN_INVALID")
    return Domain(text)


def _target_class(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationFailed("the new classification is required: " + ", ".join(CLASSES),
                               code="CLASSIFICATION_REQUIRED", details={"allowed": list(CLASSES)})
    if not isinstance(value, str):
        raise ValidationFailed("the new classification must be text: " + ", ".join(CLASSES),
                               code="INVALID_CLASSIFICATION", details={"allowed": list(CLASSES)})
    return cls.normalize(value)  # INVALID_CLASSIFICATION for an unknown label


def normalized(label: Any) -> str:
    """A stored class label as routing reads it: a known class, or RESTRICTED for anything else (ADR-0133)."""
    text = str(label or "").strip().upper()
    return text if text in CLASSES else STRICTEST


def class_changes(session: Session, project_id: str) -> list[tuple[datetime | None, str, str]]:
    """(when, from, to) of every recorded change of the project's class, oldest first, from the append-only audit
    trail (``portfolio.project.reclassify`` SUCCESS records: the command and a restoring revocation)."""
    from aeos.models.audit import AuditEvent

    rows = session.scalars(select(AuditEvent).where(
        AuditEvent.action == ACTION, AuditEvent.result == "SUCCESS", AuditEvent.project_id == project_id)
        .order_by(AuditEvent.seq))
    return [(ensure_aware(r.at), normalized((r.details or {}).get("from")), normalized((r.details or {}).get("to")))
            for r in rows]


def class_resolver(session: Session, project: Project) -> Callable[[Any], str]:
    """``class_at(when)``: the project's class in effect at *when* (the ``from`` of the first change after it, or the
    current class), so a record created while the project was INTERNAL stays INTERNAL after a lowering. ``when`` None
    (origin time unknown) -> the highest class the project ever had (ADR-0133, fail closed)."""
    current = normalized(project.data_classification)
    changes = class_changes(session, project.id)
    highest = max([current, *[c for _at, frm, to in changes for c in (frm, to)]], key=cls.rank)

    def class_at(when: Any) -> str:
        at = ensure_aware(when) if isinstance(when, datetime) else None
        if at is None:
            return highest
        for changed_at, frm, _to in changes:
            if changed_at is None or changed_at > at:
                return frm
        return current

    return class_at


def held_above(session: Session, project_id: str, klass: str) -> dict[str, int]:
    """Memory records, attachments and evidence of the project whose own class is above *klass*: after a lowering they
    keep their class, so they never reach a task or route of a lower class (ADR-0133)."""
    from aeos.models.evidence import EvidenceArtifact
    from aeos.models.portfolio import Attachment
    from aeos.models.research import MemoryRecord

    out: dict[str, int] = {}
    for name, model in (("memory", MemoryRecord), ("attachments", Attachment), ("evidence", EvidenceArtifact)):
        labels = session.scalars(select(model.classification).where(model.project_id == project_id))
        out[name] = sum(1 for label in labels if cls.rank(normalized(label)) > cls.rank(klass))
    return out


def in_flight(session: Session, project_id: str) -> dict[str, Any]:
    """TaskSpecs of the project by class, and those still open (planned, queued, running, in review, blocked)."""
    from aeos.models.planning import TaskSpec
    from aeos.models.workforce import TaskExecution

    terminal = {s.value for s in TERMINAL_EXECUTION_STATES}
    rows = session.execute(
        select(TaskSpec.data_classification, TaskExecution.state)
        .outerjoin(TaskExecution, TaskExecution.task_id == TaskSpec.id)
        .where(TaskSpec.project_id == project_id)).all()
    by_class: dict[str, int] = {}
    open_by_class: dict[str, int] = {}
    for klass, state in rows:
        by_class[klass] = by_class.get(klass, 0) + 1
        if state not in terminal:  # no execution row yet = planned, still open
            open_by_class[klass] = open_by_class.get(klass, 0) + 1
    order = {c: i for i, c in enumerate(CLASSES)}

    def ordered(counts: dict[str, int]) -> dict[str, int]:
        return dict(sorted(counts.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0])))

    return {"task_specs": len(rows), "open_task_specs": sum(open_by_class.values()), "by_class": ordered(by_class),
            "open_by_class": ordered(open_by_class)}



def _check_actor(session: Session, *, actor: User | None, actor_user_id: str, project: Project,
                 direction: str) -> User:
    if actor is None or not actor.is_active:
        _deny(session, actor_id=actor_user_id, project=project, code="RECLASSIFY_ACTOR_UNKNOWN",
              reason="the actor is not an active user; a classification change names an active SUPER_ADMIN")
    if actor.role != Role.SUPER_ADMIN.value:
        if direction == LOWER:
            _deny(session, actor_id=actor.id, project=project, code="LOWERING_NEEDS_SUPER_ADMIN",
                  reason=f"lowering a data classification is a POLICY decision: only an active SUPER_ADMIN may "
                         f"record it (actor {actor.username} is {actor.role})")
        _deny(session, actor_id=actor.id, project=project, code="RECLASSIFY_READ_ONLY_ACTOR",
              reason=f"actor {actor.username} is {actor.role} (read-only); raising a classification needs an "
                     "active SUPER_ADMIN")
    if (actor.domain_scope is not None and project.domain not in actor.domain_scope) or (
            actor.project_scope is not None and project.id not in actor.project_scope):
        # §20: SUPER_ADMIN is not automatically AUTH over every domain or project.
        _deny(session, actor_id=actor.id, project=project, code="RECLASSIFY_SCOPE_DENIED",
              reason="the actor's domain or project scope does not include this project")
    return actor


def reclassify_project(session: Session, *, project: Project, domain: Domain | str, to: Any, reason: Any,
                       actor_user_id: str, approval_ref: Any = None) -> dict[str, Any]:
    """Change *project*'s data classification to *to* (see the module docstring). Does not commit the change.

    Returns the result the CLI prints: from/to/direction, the decision (lowering), the ended decisions (raise), the
    audit record id, the actor, the reason, the owner's approval reference (*approval_ref*, optional: where and when the
    owner approved, e.g. "chat 2026-10-28, owner rule R43"; stored in the decision and the audit record) and the
    in-flight TaskSpec counts that keep their class."""
    chosen = _domain(domain)
    if project.domain != chosen.value:
        raise NotFound(f"project not found in {chosen.value}")  # another domain's project is never revealed
    new = _target_class(to)
    raw_old = str(project.data_classification or "").strip()
    old = normalized(raw_old)  # an unknown stored label is RESTRICTED, as routing reads it (fail closed)
    from_label = None if raw_old.upper() == old else raw_old[:64]
    text = clean_text(reason, field="reason", max_len=MAX_DESCRIPTION, required=True)
    if len(text) < MIN_REASON:
        raise ValidationFailed(f"the reason needs at least {MIN_REASON} characters (name the owner's approval)",
                               code="REASON_TOO_SHORT")
    reason_v = redact_text(text)
    approval = redact_text(clean_text(approval_ref, field="approval_ref", max_len=MAX_APPROVAL_REF)) or None
    base = {"project_id": project.id, "project_key": project.project_key, "domain": project.domain, "from": old,
            "from_label": from_label, "to": new, "reason": reason_v, "approval_ref": approval, "event": EVENT,
            "in_flight_rule": IN_FLIGHT_RULE}
    actor = session.get(User, actor_user_id) if actor_user_id else None
    if new == old and from_label is None:
        if actor is None or not actor.is_active:
            raise PolicyDenied("the actor is not an active user", code="RECLASSIFY_ACTOR_UNKNOWN")
        return {**base, "direction": UNCHANGED, "actor": actor.username, "decision_id": None, "ended_decisions": [],
                "audit_id": None, "in_flight": in_flight(session, project.id)}
    # from an unknown stored label (read as RESTRICTED) any other class is a lowering; RESTRICTED itself normalises it
    direction = LOWER if cls.rank(new) < cls.rank(old) else RAISE
    if direction == LOWER and project.domain == Domain.OFFICE.value:
        _deny(session, actor_id=actor_user_id, project=project, code="OFFICE_LOWERING_DENIED", reason=OFFICE_REFUSAL,
              details={"from": old, "to": new})
    user = _check_actor(session, actor=actor, actor_user_id=actor_user_id, project=project, direction=direction)
    if direction == LOWER:
        require_writable(project)  # an ARCHIVED/RETIRED project is read-only; raising stays possible
    flight = in_flight(session, project.id)
    above = held_above(session, project.id, new) if direction == LOWER else {}
    decision_id: str | None = None
    restore_to: str | None = None
    ended: list[str] = []
    if direction == LOWER:
        decision, ended = policy.record_classification_lowering(
            session, project_id=project.id, project_key=project.project_key, domain=project.domain, from_class=old,
            to_class=new, actor_user_id=user.id, reason=reason_v,
            details={"project_key": project.project_key, "is_platform": bool(project.is_platform),
                     "actor_username": user.username,
                     # stated, never implied: the CLI names the actor and asks no password; the owner's approval
                     # (chat, owner rule R43) is what allows it, and approval_ref says where it was given
                     "actor_check": ACTOR_CHECK, "approval_ref": approval, "from_label": from_label,
                     "held_above_new_class": above, "in_flight": flight, "in_flight_rule": IN_FLIGHT_RULE})
        decision_id = decision.id
        restore_to = str((decision.details or {}).get("restore_to") or old)
    project.data_classification = new
    session.flush()
    if direction == RAISE:
        # after the class changed: the lifecycle hook restores a class only while a decision still backs it
        ended = policy.end_classification_lowerings(
            session, project_id=project.id, actor_user_id=user.id,
            reason=f"classification raised to {new} by {user.username}: {reason_v}")
    rec = audit.record(
        session, actor_type="user", actor_id=user.id, action=ACTION, target_type="project", target_id=project.id,
        project_id=project.id, domain=project.domain, reason=reason_v,
        details={"from": old, "to": new, "direction": direction, "actor_username": user.username,
                 "approval_ref": approval, "actor_check": ACTOR_CHECK, "decision_id": decision_id,
                 "ended_decisions": ended, "from_label": from_label, "held_above_new_class": above,
                 "in_flight": flight, "in_flight_rule": IN_FLIGHT_RULE})
    events.emit(session, source="portfolio", kind=EVENT, entity_type="project", entity_id=project.id,
                project_id=project.id, domain=project.domain,
                payload={"project_key": project.project_key, "from": old, "to": new, "direction": direction,
                         "actor": user.username, "reason": reason_v, "approval_ref": approval,
                         "decision_id": decision_id, "ended_decisions": ended})
    return {**base, "direction": direction, "actor": user.username, "decision_id": decision_id,
            "restore_to": restore_to, "ended_decisions": ended, "audit_id": rec.event_id, "in_flight": flight,
            "held_above_new_class": above}


def on_decision_inactive(session: Session, decision: Decision) -> dict[str, Any] | None:
    """A lowering decision that was revoked or expired while it still backs the project's class restores the class
    before its chain of lowerings (``restore_to``: a lowering that superseded an earlier one carries the class before
    the first; older decisions without it restore their ``from``) - fail closed. SUPERSEDED (a newer lowering backs the
    class) and decisions a raise already ended (the class moved on) change nothing."""
    if decision.kind != "POLICY" or decision.target_type != TARGET or decision.status not in ("INVALIDATED", "EXPIRED"):
        return None
    details = decision.details or {}
    lowered_from = details.get("restore_to") or details.get("from_classification")
    lowered_to = details.get("to_classification")
    project = session.get(Project, decision.target_id)
    if project is None or lowered_to not in CLASSES:
        return None
    lowered_from = normalized(lowered_from)  # an unknown or missing origin label restores the strictest class
    if project.data_classification != lowered_to or cls.rank(lowered_from) <= cls.rank(lowered_to):
        return None
    project.data_classification = lowered_from
    session.flush()
    why = (f"lowering decision {decision.id} is {decision.status}; class restored to {lowered_from}"
           f" ({decision.invalidation_reason or '-'})")
    audit.record(session, actor_type="system", actor_id="policy", action=ACTION, target_type="project",
                 target_id=project.id, project_id=project.id, domain=project.domain, reason=why,
                 details={"from": lowered_to, "to": lowered_from, "direction": RAISE, "trigger": "decision_inactive",
                          "decision_id": decision.id, "decision_status": decision.status})
    events.emit(session, source="portfolio", kind=EVENT, entity_type="project", entity_id=project.id,
                project_id=project.id, domain=project.domain,
                payload={"project_key": project.project_key, "from": lowered_to, "to": lowered_from,
                         "direction": RAISE, "actor": "policy", "reason": why, "decision_id": decision.id,
                         "ended_decisions": [decision.id]})
    return {"project_id": project.id, "from": lowered_to, "to": lowered_from, "decision_id": decision.id}


def _counts(by_class: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in by_class.items()) or "none"


def summary_lines(result: dict[str, Any]) -> list[str]:
    """What the CLI prints (ASCII; the caller makes user text ASCII-safe)."""
    key, dom, old, new = result["project_key"], result["domain"], result["from"], result["to"]
    flight = result.get("in_flight") or {}
    if result["direction"] == UNCHANGED:
        return [f"Project {key} ({dom}) is already {new}; nothing was recorded."]
    lines: list[str] = []
    if result.get("from_label"):
        lines.append(f"The stored class {result['from_label']!r} is not a known class; it was read as {old} "
                     "(the strictest), as routing reads it.")
    if result["direction"] == LOWER:
        lines.append(f"Project {key} ({dom}): {old} -> {new} (lowered).")
        lines.append(f"Recorded POLICY decision {result['decision_id']} on {TARGET}:{result['project_id']}; "
                     f"audit {result['audit_id']}; event {EVENT}.")
    else:
        lines.append(f"Project {key} ({dom}): {old} -> {new} (raised; audit record, no decision needed).")
        lines.append(f"Audit {result['audit_id']}; event {EVENT}.")
    if result.get("ended_decisions"):
        state = "SUPERSEDED" if result["direction"] == LOWER else "INVALIDATED"
        lines.append(f"Earlier lowering decision(s) {', '.join(result['ended_decisions'])}: {state}.")
    lines.append(f"Actor {result['actor']}; reason: {result['reason']}")
    lines.append(f"Owner approval: {result.get('approval_ref') or 'not given (--approval-ref)'}")
    lines.append(f"In-flight work keeps its class: {flight.get('task_specs', 0)} existing TaskSpec(s) "
                 f"({flight.get('open_task_specs', 0)} open), by class {_counts(flight.get('by_class') or {})}; "
                 "none is changed (no silent downgrade of in-flight work).")
    lines.append(f"Only plans and task DAGs compiled after this change use {new} as their floor: a task with no "
                 f"stated class gets {new}, a stated class can only raise it.")
    if result["direction"] == LOWER:
        above = result.get("held_above_new_class") or {}
        lines.append(f"Classified above {new} and kept so: {above.get('memory', 0)} memory record(s), "
                     f"{above.get('attachments', 0)} attachment(s), {above.get('evidence', 0)} evidence item(s); "
                     f"they are never put into the context of a task below their class or sent to a route that does "
                     f"not allow their class. Project records created before this change keep the class they had.")
        restore = result.get("restore_to") or old
        lines.append(f"Undo: run this command again with --to {restore}, or revoke the decision "
                     f"(aeos policy revoke {result['decision_id']}), which restores {restore}.")
    else:
        below = {k: v for k, v in (flight.get("open_by_class") or {}).items() if cls.rank(k) < cls.rank(new)}
        if below:
            lines.append(f"Open tasks below {new} ({_counts(below)}) are routed as {new} from now on (routing "
                         "applies the higher of a task's class and the project's class); their TaskSpecs are "
                         "unchanged.")
    return lines
