"""Cross-module contract for change proposals (§39-§41). Owned by ``aeos.proposals``.

Other modules (inventory "Usulkan perubahan", evaluation scorecards, research update notices)
open proposals here; nobody but a human decision changes their status beyond SUBMITTED/REVIEWED.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.decisions import Decision
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
    """ACTIVE TRIAL_APPROVE/PROMOTE decision still bound to the live baseline/candidate, else None."""
    from aeos.proposals.service import verify_binding as impl

    p = session.get(ChangeProposal, proposal_id)
    return None if p is None else impl(session, p, action=action)
