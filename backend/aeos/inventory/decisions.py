"""Decision-side §38 dimensions of a component, derived from the Decision registry (§13, §20, §38-§41).

The Decision registry is the only authority. Admission and final lifecycle are therefore computed
from the human decisions that bind a component — never from seed data, model output or an
observation — and the ``ComponentReference`` columns are only the cache of the last explicit
component decision (``set_admission`` / ``set_lifecycle``):

* **admission** — ChangeProposal decisions for components listed as *candidates* (ACTIVE TRIAL ->
  TRIAL_APPROVED, ACTIVE PROMOTION -> APPROVED, ACTIVE REJECTION -> REJECTED, REVISION_REQUEST or an
  open proposal -> UNDER_REVIEW) and explicit component decisions (``target_type="component"``,
  ``details.admission``, e.g. SUSPENDED after a security finding);
* **lifecycle** — REMOVE/REPLACE proposals for components listed as *existing*: an ACTIVE PROMOTION
  makes the old component RETIRING, a recorded ACCEPTED outcome makes it RETIRED (REMOVE) or
  DEPRECATED (REPLACE), a ROLLED_BACK outcome returns it to ACTIVE; explicit component decisions
  (``details.lifecycle_final``, e.g. QUARANTINED) and manifest tombstones (RETIRED, history kept).

Every derived value carries the decision ids, the binding hash and the scope it came from. Only
decisions of the viewer's domain (or domain-less platform decisions) are used: an OFFICE proposal
never changes what a PERSONAL session sees (§23, §35). Nothing here deletes a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import AdmissionStatus, LifecycleFinal
from aeos.core.errors import NotFound, PolicyDenied, ValidationFailed
from aeos.core.timeutil import ensure_aware
from aeos.models.decisions import Decision
from aeos.models.inventory import ComponentReference

COMPONENT_TARGET = "component"
PROPOSAL_TARGET = "change_proposal"
HUMAN_ACTORS = frozenset({"HUMAN", "HUMAN_RECORDED"})
ADMISSIONS = frozenset(s.value for s in AdmissionStatus)
LIFECYCLES = frozenset(s.value for s in LifecycleFinal)
# Decision kind (proposal decision) -> admission of the candidate component.
ADMISSION_BY_KIND = {"PROMOTION": "APPROVED", "TRIAL": "TRIAL_APPROVED", "REJECTION": "REJECTED",
                     "REVISION_REQUEST": "UNDER_REVIEW"}
OPEN_PROPOSAL = frozenset({"SUBMITTED", "UNDER_REVIEW", "REVIEWED", "IN_TRIAL", "PROMOTION_PENDING", "DEFERRED",
                           "REQUEST_MORE_EVIDENCE"})
RETIRING_KINDS = frozenset({"REMOVE", "REPLACE"})
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class DecisionState:
    """Derived admission/lifecycle of one component for one viewer domain."""

    admission: str = "DISCOVERED"
    admission_reason: str = "belum ada keputusan admission (katalog desain)"
    admission_at: datetime | None = None
    admission_source: str = "seed manifest"
    lifecycle: str = "ACTIVE"
    lifecycle_reason: str = "status lifecycle"
    lifecycle_at: datetime | None = None
    lifecycle_source: str = "manifest"
    decision_refs: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    previous_accepted_configuration: dict[str, Any] | None = None
    scope: dict[str, Any] = field(default_factory=dict)


def _iso(dt: datetime | None) -> str | None:
    dt = ensure_aware(dt)
    return dt.isoformat() if dt else None


def _visible(decision_domain: str | None, domain: str | None) -> bool:
    return decision_domain is None or decision_domain == domain


def decision_view(d: Decision, *, via: str, proposal: Any | None = None) -> dict[str, Any]:
    det = d.details or {}
    return {
        "id": d.id, "kind": d.kind, "status": d.status, "actor_kind": d.actor_kind, "via": via,
        "target_type": d.target_type, "target_id": d.target_id, "target_version": d.target_version,
        # §37 "approval target/hash": the exact binding the human decided on
        "binding_hash": d.target_hash, "baseline_hash": det.get("baseline_hash"),
        "candidate_hash": det.get("candidate_hash"), "action": det.get("action"),
        "scope": d.auth_scope or {}, "domain": d.domain, "created_at": _iso(d.created_at),
        "proposal_id": proposal.id if proposal is not None else None,
        "proposal_kind": proposal.kind if proposal is not None else None,
    }


def decision_states(session: Session, comps: dict[str, ComponentReference], *, domain: str | None
                    ) -> dict[str, DecisionState]:
    """component_id -> DecisionState for *comps* as seen from *domain* (one query per source)."""
    from aeos.models.research import ChangeProposal

    out: dict[str, DecisionState] = {}
    for cid, c in comps.items():
        st = DecisionState(admission=c.admission_status or "DISCOVERED", lifecycle=c.lifecycle_final or "ACTIVE",
                           decision_refs=list(c.decision_refs or []))
        if c.decision_refs:
            st.admission_reason = "terikat keputusan yang tercatat"
            st.admission_source = "decision registry"
        if c.tombstoned_at is not None:
            st.lifecycle, st.lifecycle_at = "RETIRED", c.tombstoned_at
            st.lifecycle_reason = "dihapus dari manifest (tombstone); histori dipertahankan"
        out[cid] = st
    if not comps:
        return out
    # (1) explicit component decisions (security SUSPEND/QUARANTINE, owner lifecycle decisions)
    direct = session.scalars(select(Decision).where(Decision.target_type == COMPONENT_TARGET,
                                                    Decision.target_id.in_(list(comps)))
                             .order_by(Decision.created_at))
    events: dict[str, list[tuple[datetime | None, str, str, str, dict[str, Any]]]] = {}
    for d in direct:
        if d.status != "ACTIVE" or d.actor_kind not in HUMAN_ACTORS or not _visible(d.domain, domain):
            continue
        det = d.details or {}
        view = decision_view(d, via="component")
        st = out[d.target_id]
        st.decisions.append(view)
        if det.get("admission") in ADMISSIONS:
            events.setdefault(d.target_id, []).append(
                (d.created_at, "admission", str(det["admission"]), f"keputusan {d.kind} {d.id[:8]} pada komponen",
                 view))
        if det.get("lifecycle_final") in LIFECYCLES:
            events.setdefault(d.target_id, []).append(
                (d.created_at, "lifecycle", str(det["lifecycle_final"]),
                 f"keputusan {d.kind} {d.id[:8]} pada komponen", view))
    # (2) ChangeProposal decisions (candidates -> admission, existing REMOVE/REPLACE -> lifecycle)
    proposals = [p for p in session.scalars(select(ChangeProposal).where(ChangeProposal.parent_id.is_(None)))
                 if _visible(p.domain, domain)
                 and (set(p.candidate_component_ids or []) | set(p.existing_component_ids or [])) & set(comps)]
    by_proposal: dict[str, list[Decision]] = {}
    if proposals:
        for d in session.scalars(select(Decision).where(Decision.target_type == PROPOSAL_TARGET,
                                                        Decision.target_id.in_([p.id for p in proposals]))
                                 .order_by(Decision.created_at)):
            if d.actor_kind in HUMAN_ACTORS:
                by_proposal.setdefault(d.target_id, []).append(d)
    for p in proposals:
        decisions = by_proposal.get(p.id, [])
        active = [d for d in decisions if d.status == "ACTIVE"]
        views = [decision_view(d, via="proposal", proposal=p) for d in decisions]
        outcome = (p.outcome or {}).get("current") or {}
        for cid in set(p.candidate_component_ids or []) & set(comps):
            st = out[cid]
            st.decisions += [v for v in views if v not in st.decisions]
            last = active[-1] if active else None
            if last is not None and last.kind in ADMISSION_BY_KIND and p.kind != "COMPARE":
                events.setdefault(cid, []).append(
                    (last.created_at, "admission", ADMISSION_BY_KIND[last.kind],
                     f"proposal {p.kind} {p.id[:8]}: keputusan {last.kind} {last.id[:8]}",
                     decision_view(last, via="proposal", proposal=p)))
            elif p.status in OPEN_PROPOSAL:
                events.setdefault(cid, []).append(
                    (p.updated_at, "admission", "UNDER_REVIEW", f"proposal {p.kind} {p.id[:8]} sedang ditinjau "
                     f"({p.status}); belum ada keputusan", {}))
        for cid in set(p.existing_component_ids or []) & set(comps):
            st = out[cid]
            st.decisions += [v for v in views if v not in st.decisions]
            if p.kind not in RETIRING_KINDS:
                continue
            promotion = next((d for d in reversed(active) if d.kind == "PROMOTION"), None)
            if outcome.get("status") == "ACCEPTED":
                target = "RETIRED" if p.kind == "REMOVE" else "DEPRECATED"
                at = _parse(outcome.get("at"))
                events.setdefault(cid, []).append(
                    (at, "lifecycle", target, f"outcome ACCEPTED proposal {p.kind} {p.id[:8]} "
                     f"(keputusan {str(outcome.get('promotion_decision_id') or '')[:8]}); baris tidak dihapus",
                     decision_view(promotion, via="proposal", proposal=p) if promotion else {}))
            elif outcome.get("status") == "ROLLED_BACK":
                events.setdefault(cid, []).append(
                    (_parse(outcome.get("at")), "lifecycle", "ACTIVE",
                     f"proposal {p.kind} {p.id[:8]} di-rollback; komponen lama tetap berlaku", {}))
            elif promotion is not None:
                events.setdefault(cid, []).append(
                    (promotion.created_at, "lifecycle", "RETIRING",
                     f"proposal {p.kind} {p.id[:8]}: PROMOTION {promotion.id[:8]} disetujui; menunggu outcome",
                     decision_view(promotion, via="proposal", proposal=p)))
        if outcome.get("status") == "ACCEPTED" and outcome.get("accepted_configuration"):
            for cid in (set(p.candidate_component_ids or []) | set(p.existing_component_ids or [])) & set(comps):
                prev = out[cid].previous_accepted_configuration
                if prev is None or str(outcome.get("at") or "") >= str(prev.get("at") or ""):
                    out[cid].previous_accepted_configuration = {
                        "value": outcome.get("accepted_configuration"), "status": "RECORDED",
                        "at": outcome.get("at"), "proposal_id": p.id,
                        "promotion_decision_id": outcome.get("promotion_decision_id"),
                        "reason": "konfigurasi terakhir yang diterima (outcome ACCEPTED bertanda bukti)"}
    for cid, evs in events.items():
        st = out[cid]
        for at, dim, value, reason, view in sorted(evs, key=lambda e: ensure_aware(e[0]) or EPOCH):
            if dim == "admission":
                st.admission, st.admission_reason, st.admission_at = value, reason, at
                st.admission_source = "decision registry" if view else "proposal registry"
                st.scope = dict(view.get("scope") or {}) if view else {}
            elif comps[cid].tombstoned_at is None or value == "RETIRED":
                st.lifecycle, st.lifecycle_reason, st.lifecycle_at = value, reason, at
                st.lifecycle_source = "decision registry" if view else "proposal registry"
            if view and view.get("id") and view["id"] not in st.decision_refs:
                st.decision_refs.append(view["id"])
    return out


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def decision_state(session: Session, comp: ComponentReference, *, domain: str | None) -> DecisionState:
    return decision_states(session, {comp.component_id: comp}, domain=domain)[comp.component_id]


# ---------------------------------------------------------------- explicit component decisions (writes)


def _bound_decision(session: Session, component_id: str, decision_id: str) -> Decision:
    """The decision must exist, be ACTIVE, be human and bind this exact component (directly, or through a
    ChangeProposal that names it)."""
    from aeos.models.research import ChangeProposal

    d = session.get(Decision, decision_id)
    if d is None:
        raise NotFound("decision not found")
    if d.status != "ACTIVE":
        raise PolicyDenied(f"decision {decision_id} is {d.status}, not ACTIVE", code="DECISION_NOT_ACTIVE")
    if d.actor_kind not in HUMAN_ACTORS:
        raise PolicyDenied("only a human decision changes admission/lifecycle", code="DECISION_ACTOR_NOT_HUMAN")
    if d.target_type == COMPONENT_TARGET and d.target_id == component_id:
        return d
    if d.target_type == PROPOSAL_TARGET:
        p = session.get(ChangeProposal, d.target_id)
        if p is not None and component_id in (p.candidate_component_ids or []) + (p.existing_component_ids or []):
            return d
    raise PolicyDenied("decision is not bound to this component", code="DECISION_TARGET_MISMATCH")


def _apply(session: Session, component_id: str, *, column: str, value: str, allowed: frozenset[str],
           decision_id: str, actor: str, kind: str) -> ComponentReference:
    from aeos.audit import service as audit
    from aeos.events import service as events
    from aeos.inventory.observations import component_domain

    if value not in allowed:
        raise ValidationFailed(f"{column} must be one of {sorted(allowed)}", code="INVALID_DECISION_VALUE")
    comp = session.get(ComponentReference, component_id)
    if comp is None:
        raise NotFound("component not found")
    d = _bound_decision(session, component_id, decision_id)
    before = getattr(comp, column)
    setattr(comp, column, value)
    if d.id not in (comp.decision_refs or []):
        comp.decision_refs = [*(comp.decision_refs or []), d.id]
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor, action=f"inventory.{kind}.change",
                 target_type="component", target_id=component_id, domain=d.domain,
                 details={"from": before, "to": value, "decision_id": d.id, "binding_hash": d.target_hash})
    events.emit(session, source="inventory", kind=f"inventory.{kind}.changed", entity_type="component",
                entity_id=component_id, domain=d.domain or component_domain(session, component_id),
                payload={"from": before, "to": value, "decision_id": d.id})
    return comp


def set_admission(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache an admission bound to an ACTIVE human decision (audited, ``inventory.admission.changed``)."""
    return _apply(session, component_id, column="admission_status", value=status, allowed=ADMISSIONS,
                  decision_id=decision_id, actor=actor, kind="admission")


def set_lifecycle(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache a final-lifecycle state bound to an ACTIVE human decision. Never deletes rows or history."""
    return _apply(session, component_id, column="lifecycle_final", value=status, allowed=LIFECYCLES,
                  decision_id=decision_id, actor=actor, kind="lifecycle")


MAINTAINER_PREFIX = "inventory.maintainer."


def set_maintainer(session: Session, *, component_id: str, name: str | None, actor: str, reason: str = ""
                   ) -> dict[str, Any] | None:
    """Owner-edited operational maintainer of a component (§37 Scope), audited; ``None`` clears it.
    A maintainer is metadata only: it grants no permission and is never guessed from upstream data."""
    from aeos.audit import service as audit
    from aeos.core.redaction import redact_text
    from aeos.core.timeutil import utcnow
    from aeos.events import service as events
    from aeos.models.identity import SystemSetting

    comp = session.get(ComponentReference, component_id)
    if comp is None:
        raise NotFound("component not found")
    clean = redact_text((name or "").strip())[:128] or None
    key = MAINTAINER_PREFIX + component_id
    row = session.get(SystemSetting, key)
    before = row.value if row is not None else None
    value = {"name": clean, "set_by": actor, "set_at": utcnow().isoformat(), "reason": redact_text(reason)[:300]} \
        if clean else None
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    session.flush()
    from aeos.inventory.observations import component_domain

    dom = component_domain(session, component_id)
    audit.record(session, actor_type="user", actor_id=actor, action="inventory.maintainer.set",
                 target_type="component", target_id=component_id, reason=reason[:300], domain=dom,
                 details={"before": (before or {}).get("name") if isinstance(before, dict) else None, "after": clean})
    events.emit(session, source="inventory", kind="inventory.component.maintainer", entity_type="component",
                entity_id=component_id, domain=dom, payload={"maintainer": clean})
    return value


def record_component_decision(session: Session, *, component_id: str, actor_user_id: str, rationale: str,
                              admission: str | None = None, lifecycle_final: str | None = None,
                              domain: str | None = None, scope: dict[str, Any] | None = None) -> Decision:
    """Record a human decision on one component (e.g. SUSPEND after a security finding, QUARANTINE, owner
    retirement) through the policy decision registry — bound to the component id and its current manifest
    hash — and cache it on the ComponentReference. The policy registry enforces that the actor is an
    active SUPER_ADMIN (agents/models never decide)."""
    from aeos.policy import facade as policy

    if admission is None and lifecycle_final is None:
        raise ValidationFailed("admission or lifecycle_final is required")
    if admission is not None and admission not in ADMISSIONS:
        raise ValidationFailed(f"admission must be one of {sorted(ADMISSIONS)}")
    if lifecycle_final is not None and lifecycle_final not in LIFECYCLES:
        raise ValidationFailed(f"lifecycle_final must be one of {sorted(LIFECYCLES)}")
    if not (rationale or "").strip():
        raise ValidationFailed("rationale is required")
    comp = session.get(ComponentReference, component_id)
    if comp is None:
        raise NotFound("component not found")
    details: dict[str, Any] = {"component_kind": comp.kind}
    if admission is not None:
        details["admission"] = admission
    if lifecycle_final is not None:
        details["lifecycle_final"] = lifecycle_final
    d = policy.record_decision(
        session, kind="POLICY", actor_user_id=actor_user_id, target_type=COMPONENT_TARGET, target_id=component_id,
        target_version=comp.approved_pin, target_hash=comp.manifest_hash,
        allowed_operations=["inventory.component.decision"], auth_scope=dict(scope or {}), domain=domain,
        rationale=rationale, details=details)
    if admission is not None:
        set_admission(session, component_id=component_id, status=admission, decision_id=d.id, actor=actor_user_id)
    if lifecycle_final is not None:
        set_lifecycle(session, component_id=component_id, status=lifecycle_final, decision_id=d.id,
                      actor=actor_user_id)
    return d
