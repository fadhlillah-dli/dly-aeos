"""Cross-module contract for change proposals (§39-§41). Owned by ``aeos.proposals``.

Other modules (inventory "Usulkan perubahan", evaluation scorecards, research update notices, the
workforce scheduler, controller release promotion, worker runtime suggestions) use these functions;
nobody but a human decision changes a proposal's status beyond SUBMITTED/REVIEWED.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.decisions import Decision
from aeos.models.planning import TaskSpec
from aeos.models.research import ChangeProposal


def create_proposal(
    session: Session, *, actor_user_id: str, domain: str, kind: str, title: str | None, sources: list[str],
    problem: str = "", expected_benefit: str = "", existing_component_ids: list[str] | None = None,
    limits: dict[str, Any] | None = None,
) -> ChangeProposal:
    from aeos.proposals.service import create_proposal as impl

    return impl(session, actor_user_id=actor_user_id, domain=domain, kind=kind, title=title, sources=sources,
                problem=problem, expected_benefit=expected_benefit, existing_component_ids=existing_component_ids,
                limits=limits)


def proposals_for_component(session: Session, component_id: str, *, domain: str) -> list[dict[str, Any]]:
    from aeos.proposals.service import list_proposals

    return list_proposals(session, domain=domain, allowed_project_ids=None, component_id=component_id)


def verify_binding(session: Session, proposal_id: str, *, action: str) -> Decision | None:
    """ACTIVE TRIAL_APPROVE/PROMOTE decision still bound to the live baseline/candidate/scope, else None
    (stale decisions are invalidated)."""
    from aeos.proposals.service import verify_binding as impl

    p = session.get(ChangeProposal, proposal_id)
    return None if p is None else impl(session, p, action=action)


# ---- additive (non-breaking) contract -------------------------------------------------------------


def trial_admission_problems(session: Session, task: TaskSpec, *, route: Any = None) -> list[tuple[str, str]]:
    """Scheduler admission: blocks for an attempt of a TaskSpec created for a proposal trial (empty for any
    other TaskSpec). Re-verifies the TRIAL binding, the decided provider/operations and the caps."""
    from aeos.proposals.trial import admission_problems

    return admission_problems(session, task, route=route)


def trial_authorisation_lost(session: Session, task_id: str | None) -> str | None:
    """Scheduler stop conditions: why a running proposal-trial attempt is no longer authorised (None = fine)."""
    from aeos.proposals.trial import authorisation_lost

    return authorisation_lost(session, task_id)


def promotion_for_release(session: Session, proposal_id: str, *, domain: str | None = None) -> Decision | None:
    """Controller release promotion gated by a proposal: the ACTIVE PROMOTION decision still bound to the
    candidate (baseline drift limited to the promoted change), else None."""
    from aeos.proposals.service import verify_binding as impl

    p = session.get(ChangeProposal, proposal_id)
    if p is None or (domain is not None and p.domain != domain):
        return None
    return impl(session, p, action="PROMOTE", after_promotion=True)


def link_release(session: Session, proposal_id: str, release_id: str, *, actor: str) -> None:
    """Record a promoted controller release on the proposal (§40 "release records")."""
    from aeos.audit import service as audit

    p = session.get(ChangeProposal, proposal_id)
    if p is None:
        return
    links = dict(p.links or {})
    links["release_ids"] = sorted({*(links.get("release_ids") or []), release_id})
    p.links = links
    session.flush()
    audit.record(session, actor_type="cli", actor_id=actor, action="proposal.links.release",
                 target_type="change_proposal", target_id=p.id, domain=p.domain, details={"release_id": release_id})


def file_suggestion(session: Session, *, source_kind: str, source_id: str, domain: str, project_id: str | None,
                    kind: str, title: str | None, sources: list[str], rationale: str,
                    existing_component_ids: list[str] | None = None) -> ChangeProposal:
    """§24: a skill/memory/tool suggestion from Hermes or another worker becomes an untrusted SUBMITTED
    proposal for human review - never active policy, memory or a decision."""
    from aeos.proposals.service import file_suggestion as impl

    return impl(session, source_kind=source_kind, source_id=source_id, domain=domain, project_id=project_id, kind=kind,
                title=title, sources=sources, rationale=rationale, existing_component_ids=existing_component_ids)
