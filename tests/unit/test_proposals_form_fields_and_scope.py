"""Change proposal form fields, review outcomes and project scope (master §39 form minimum, §39 review
outcomes KEEP/REJECT/DEFER/REQUEST_MORE_EVIDENCE, §39 "platform atau proyek/domain")."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aeos.core.enums import Domain
from aeos.models.decisions import Decision
from aeos.models.research import ChangeProposal
from aeos.proposals import review as review_mod
from aeos.proposals import service
from tests.conftest import AuthedClient, create_viewer
from tests.unit.proposal_fixtures import create as _create
from tests.unit.proposal_fixtures import decide as _decide
from tests.unit.proposal_fixtures import review as _review
from tests.unit.test_portfolio_support import create_project
from tests.unit.test_proposals_service import policy  # noqa: F401

LIMITS = {"cost_usd": 1, "max_minutes": 30, "max_attempts": 2, "data_class": "INTERNAL", "resource": "1 sandbox"}


def test_problem_benefit_and_limits_are_stored_redacted_and_shown_in_the_review(db, policy):  # noqa: F811
    secret = "ghp_" + "Q" * 36
    p = service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="ADD", title=None,
                                sources=["https://github.com/octo/tool"], problem=f"Build lambat; token {secret}",
                                expected_benefit="CI 30% lebih cepat", limits=LIMITS)
    db.commit()
    assert secret not in p.problem and "Build lambat" in p.problem
    p = _review(db, p)
    summary = p.review_package["summary"]
    assert summary["problem"] == p.problem and summary["expected_benefit"] == "CI 30% lebih cepat"
    assert p.review_package["experiment_plan"]["limits"] == LIMITS
    cash = p.review_package["cost"]["cash"]
    assert cash["cap_usd"] == 1 and cash["value"] is None and cash["basis"] == "UNKNOWN"  # a cap, not an estimate


def test_detail_shows_when_a_candidate_became_active_from_inventory_not_approval(db):
    """§41 detail 'kapan aktif': the first ENABLED inventory observation of a candidate, never the approval."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.models.inventory import ComponentObservation, ComponentReference

    p = service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="ADD", title=None,
                                sources=["https://github.com/octo/tool"], candidate_component_ids=["toolx"])
    db.commit()
    assert service.delivery_dimensions(db, p)["ACTIVE"]["at"] is None
    db.add(ComponentReference(component_id="toolx", kind="tool", name="ToolX", enablement="ENABLED"))
    first = utcnow() - timedelta(days=2)
    db.add_all([ComponentObservation(component_id="toolx", dimension="enablement", status="ENABLED", observer="owner",
                                     observed_at=first),
                ComponentObservation(component_id="toolx", dimension="enablement", status="ENABLED", observer="owner",
                                     observed_at=utcnow())])
    db.commit()
    active = service.delivery_dimensions(db, p)["ACTIVE"]
    assert active["status"] == "ENABLED_IN_CONFIG" and active["at"].startswith(first.isoformat()[:16])


@pytest.mark.parametrize("action,status,kind", [
    ("KEEP", "KEEP", "REJECTION"),
    ("DEFER", "DEFERRED", "REVISION_REQUEST"),
    ("REQUEST_MORE_EVIDENCE", "REQUEST_MORE_EVIDENCE", "REVISION_REQUEST"),
])
def test_review_can_end_keep_defer_or_request_more_evidence(db, policy, action, status, kind):  # noqa: F811
    p = _review(db, _create(db, kind="ADD", sources=["https://github.com/octo/tool"]))
    assert "TRIAL_APPROVE" in review_mod.allowed_actions(p)
    d = _decide(db, p, action, scope={})
    db.refresh(p)
    assert p.status == status and d.kind == kind
    assert "TRIAL_APPROVE" not in review_mod.allowed_actions(p)  # a new review is needed before any trial
    assert "PROMOTE" not in review_mod.allowed_actions(p)


def test_project_scoped_proposals_check_project_domain_and_user_scope(personal_client: AuthedClient, client, db):
    p = create_project(personal_client, name="Scoped A")
    other = create_project(personal_client, name="Scoped B")
    body = {"kind": "COMPARE", "sources": ["https://github.com/octo/tool"], "scope_type": "PROJECT"}
    before = db.scalar(select(func.count()).select_from(ChangeProposal))
    r = personal_client.post("/api/proposals", json=body)  # PROJECT without project_id
    assert r.status_code in (404, 422)
    personal_client.select_domain(Domain.OFFICE)
    office = create_project(personal_client, domain=Domain.OFFICE, name="Office P")
    personal_client.select_domain(Domain.PERSONAL)
    r = personal_client.post("/api/proposals", json={**body, "project_id": office["id"]})  # other domain
    assert r.status_code == 404
    assert db.scalar(select(func.count()).select_from(ChangeProposal)) == before  # nothing created
    created = personal_client.post("/api/proposals", json={**body, "project_id": other["id"]})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    assert created.json()["scope_type"] == "PROJECT" and created.json()["project_id"] == other["id"]
    # a user scoped to project A cannot read project B's proposal
    from aeos.models.identity import User

    create_viewer(db)
    viewer = db.scalar(select(User).where(User.username == "viewer"))
    viewer.project_scope = [p["id"]]
    db.commit()
    from tests.conftest import VIEWER_PW

    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.get(f"/api/proposals/{pid}").status_code == 404
    assert pid not in {i["id"] for i in client.get("/api/proposals").json()["items"]}
    assert db.scalar(select(func.count()).select_from(Decision).where(Decision.target_id == pid)) == 0
