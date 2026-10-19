"""Decision-side §38 dimensions: admission and final lifecycle from the Decision registry, enablement from
approved configuration, and the §37 Changes group (binding hashes, previous accepted configuration).

Audit items covered (C4 inventory): §38 Admission/decision bound to decision and scope; §38 Enablement in
approved config; §38/§40 Final lifecycle (DEPRECATED/RETIRING/RETIRED/QUARANTINED, never deleting history);
§37 merge of the Decision registry; §37 Changes approval target/hash + previous accepted configuration.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.errors import PolicyDenied
from aeos.core.timeutil import utcnow
from aeos.inventory import views
from aeos.inventory.decisions import record_component_decision, set_admission
from aeos.inventory.seed import run as seed_run
from aeos.models.audit import AuditEvent, Event
from aeos.models.inventory import ComponentReference
from aeos.models.research import ChangeProposal
from tests.conftest import create_admin


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


@pytest.fixture
def owner(seeded):
    return create_admin(seeded)


def _proposal(db, *, kind="ADD", candidates=(), existing=(), domain="PERSONAL", status="REVIEWED") -> ChangeProposal:
    p = ChangeProposal(kind=kind, title=f"{kind} test", submitted_by="owner", domain=domain, status=status,
                       candidate_component_ids=list(candidates), existing_component_ids=list(existing),
                       baseline_hash="sha256:" + "b" * 64, candidate_hash="sha256:" + "c" * 64)
    db.add(p)
    db.flush()
    return p


def _decide(db, owner, p: ChangeProposal, kind: str, *, domain="PERSONAL"):
    from aeos.policy import facade as policy

    return policy.record_decision(
        db, kind=kind, actor_user_id=owner.id, target_type="change_proposal", target_id=p.id,
        target_hash="sha256:" + "d" * 64, allowed_operations=["record.decision"], domain=domain,
        rationale="owner decides in a test", details={"baseline_hash": p.baseline_hash,
                                                     "candidate_hash": p.candidate_hash, "action": kind})


def _dims(db, cid, domain="PERSONAL"):
    return views.component_detail(db, cid, domain=domain)["lifecycle"]


def test_seed_is_discovered_and_open_proposal_is_under_review(seeded, owner):
    assert _dims(seeded, "hermes")["admission"]["value"] == "DISCOVERED"
    _proposal(seeded, candidates=["hermes"], status="UNDER_REVIEW")
    seeded.commit()
    adm = _dims(seeded, "hermes")["admission"]
    assert adm["value"] == "UNDER_REVIEW" and "ditinjau" in adm["reason"]


def test_trial_promotion_rejection_drive_admission_with_decision_refs(seeded, owner):
    p = _proposal(seeded, candidates=["hermes"])
    trial = _decide(seeded, owner, p, "TRIAL")
    seeded.commit()
    adm = _dims(seeded, "hermes")["admission"]
    assert adm["value"] == "TRIAL_APPROVED" and trial.id in adm["decision_refs"]
    assert adm["source"] == "decision registry"
    promo = _decide(seeded, owner, p, "PROMOTION")
    seeded.commit()
    detail = views.component_detail(seeded, "hermes", domain="PERSONAL")
    assert detail["lifecycle"]["admission"]["value"] == "APPROVED"
    # §37 Changes: each decision with its exact binding hash
    by_id = {d["id"]: d for d in detail["changes"]["decisions"]}
    assert by_id[promo.id]["binding_hash"] == "sha256:" + "d" * 64
    assert by_id[promo.id]["candidate_hash"] == p.candidate_hash and by_id[promo.id]["proposal_id"] == p.id
    # a later rejection on another proposal wins (latest decision)
    p2 = _proposal(seeded, candidates=["hermes"])
    _decide(seeded, owner, p2, "REJECTION")
    seeded.commit()
    assert _dims(seeded, "hermes")["admission"]["value"] == "REJECTED"


def test_office_proposal_never_changes_the_personal_view(seeded, owner):
    from aeos.models.identity import User

    seeded.get(User, owner.id).domain_scope = None
    p = _proposal(seeded, candidates=["hermes"], domain="OFFICE")
    _decide(seeded, owner, p, "PROMOTION", domain="OFFICE")
    seeded.commit()
    assert _dims(seeded, "hermes", "PERSONAL")["admission"]["value"] == "DISCOVERED"
    assert _dims(seeded, "hermes", "OFFICE")["admission"]["value"] == "APPROVED"
    personal = views.component_detail(seeded, "hermes", domain="PERSONAL")["changes"]
    assert personal["proposals"] == [] and personal["decisions"] == []


def test_replace_and_remove_mark_the_old_component_without_deleting_it(seeded, owner):
    p = _proposal(seeded, kind="REMOVE", existing=["ralph"])
    _decide(seeded, owner, p, "PROMOTION")
    seeded.commit()
    life = _dims(seeded, "ralph")["lifecycle_final"]
    assert life["value"] == "RETIRING"
    p.outcome = {"current": {"status": "ACCEPTED", "at": utcnow().isoformat(),
                             "accepted_configuration": {"removed": "ralph"}, "promotion_decision_id": "x"},
                 "history": []}
    seeded.commit()
    assert _dims(seeded, "ralph")["lifecycle_final"]["value"] == "RETIRED"
    assert seeded.get(ComponentReference, "ralph") is not None, "history is kept, the row is never deleted"
    prev = views.component_detail(seeded, "ralph", domain="PERSONAL")["changes"]["previous_accepted_configuration"]
    assert prev["status"] == "RECORDED" and prev["value"] == {"removed": "ralph"} and prev["proposal_id"] == p.id
    # a rollback returns the old component to ACTIVE (reversible)
    p.outcome = {"current": {"status": "ROLLED_BACK", "at": utcnow().isoformat()}, "history": []}
    seeded.commit()
    assert _dims(seeded, "ralph")["lifecycle_final"]["value"] == "ACTIVE"
    q = _proposal(seeded, kind="REPLACE", existing=["goose"], candidates=["opencode"])
    _decide(seeded, owner, q, "PROMOTION")
    q.outcome = {"current": {"status": "ACCEPTED", "at": utcnow().isoformat(), "accepted_configuration": {"x": 1}},
                 "history": []}
    seeded.commit()
    assert _dims(seeded, "goose")["lifecycle_final"]["value"] == "DEPRECATED"


def test_tombstoned_component_reports_retired(seeded):
    seeded.get(ComponentReference, "ralph").tombstoned_at = utcnow()
    seeded.commit()
    life = _dims(seeded, "ralph")["lifecycle_final"]
    assert life["value"] == "RETIRED" and "tombstone" in life["reason"]


def test_security_suspension_is_a_bound_human_decision(seeded, owner):
    d = record_component_decision(seeded, component_id="opencode", actor_user_id=owner.id, admission="SUSPENDED",
                                  lifecycle_final="QUARANTINED", rationale="security finding: suspend until reviewed",
                                  domain="PERSONAL")
    seeded.commit()
    assert d.target_type == "component" and d.target_hash == seeded.get(ComponentReference, "opencode").manifest_hash
    dims = _dims(seeded, "opencode")
    assert dims["admission"]["value"] == "SUSPENDED" and d.id in dims["admission"]["decision_refs"]
    assert dims["lifecycle_final"]["value"] == "QUARANTINED"
    comp = seeded.get(ComponentReference, "opencode")
    assert comp.admission_status == "SUSPENDED" and d.id in comp.decision_refs  # cache bound to the decision
    assert seeded.scalar(select(AuditEvent).where(AuditEvent.action == "inventory.admission.change"))
    assert seeded.scalar(select(Event).where(Event.kind == "inventory.admission.changed"))
    nxt = views.component_detail(seeded, "opencode", domain="PERSONAL")["next_action"]
    assert nxt["code"] == "REVIEW_SUSPENSION"


def test_set_admission_refuses_unbound_inactive_or_missing_decisions(seeded, owner):
    from aeos.core.errors import NotFound

    with pytest.raises(NotFound):
        set_admission(seeded, component_id="hermes", status="APPROVED", decision_id="nope", actor="x")
    p = _proposal(seeded, candidates=["goose"])
    d = _decide(seeded, owner, p, "PROMOTION")
    with pytest.raises(PolicyDenied):  # bound to a proposal about another component
        set_admission(seeded, component_id="hermes", status="APPROVED", decision_id=d.id, actor="x")
    d.status = "INVALIDATED"
    seeded.flush()
    with pytest.raises(PolicyDenied):
        set_admission(seeded, component_id="goose", status="APPROVED", decision_id=d.id, actor="x")


def test_component_decision_endpoint_is_admin_only_and_domain_scoped(personal_client, db):
    seed_run(db)
    db.commit()
    r = personal_client.post("/api/inventory/components/opencode/decision",
                             json={"admission": "SUSPENDED", "rationale": "security review pending"})
    assert r.status_code == 201, r.text
    assert r.json()["component"]["admission"]["value"] == "SUSPENDED"
    assert personal_client.post("/api/inventory/components/jira/decision",
                                json={"admission": "SUSPENDED", "rationale": "not in this domain"}).status_code == 404
    m = personal_client.put("/api/inventory/components/opencode/maintainer",
                            json={"name": "tim platform", "reason": "owner assigns"})
    assert m.status_code == 200 and m.json()["maintainer"]["name"] == "tim platform"
    d = personal_client.get("/api/inventory/components/opencode").json()
    assert d["scope"]["maintainer"]["status"] == "SET" and d["scope"]["maintainer"]["value"] == "tim platform"


def test_enablement_follows_approved_config_not_seed(seeded, owner):
    from aeos.models.workforce import AgentDefinition

    assert _dims(seeded, "opencode")["enablement"]["value"] == "DISABLED"
    seeded.add(AgentDefinition(agent_key="impl-test", role_key="implementer", harness="opencode", enabled=True))
    seeded.commit()
    en = _dims(seeded, "opencode")["enablement"]
    assert en["value"] == "ENABLED" and any("agent definition impl-test" in b for b in en["basis"])
    assert views.enablement_for(seeded, "opencode", domain="PERSONAL")["value"] == "ENABLED"


def test_integration_alone_is_configured_not_enabled_until_write_enablement(seeded, owner):
    from aeos.models.portfolio import Project, ProjectIntegration
    from aeos.policy import facade as policy
    from aeos.policy.enablement import set_flag

    p = Project(project_key="p1", name="P1", domain="PERSONAL")
    seeded.add(p)
    seeded.flush()
    seeded.add(ProjectIntegration(project_id=p.id, domain="PERSONAL", provider="google-drive", purpose="docs",
                                  root_id="folder123"))
    seeded.commit()
    en = _dims(seeded, "google-drive")["enablement"]
    assert en["value"] == "DISABLED" and "write enablement" in en["reason"]
    policy.record_decision(seeded, kind="WRITE_ENABLEMENT", actor_user_id=owner.id, target_type="provider_write",
                           target_id="google-drive", domain="PERSONAL", rationale="owner enables Drive writes",
                           allowed_operations=["write"], details={"provider": "google-drive", "enabled": True})
    set_flag(seeded, "google-drive", True)
    seeded.commit()
    assert _dims(seeded, "google-drive")["enablement"]["value"] == "ENABLED"
