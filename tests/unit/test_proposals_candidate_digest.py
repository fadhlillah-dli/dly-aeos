"""Structured candidate digest on ChangeProposal (master §25 tool admission "pinned version/digest", §40, §47;
audit W6-075).

Before: TRIAL_APPROVE/PROMOTE carried only a free-text ``candidate_pin`` and ACCEPTED compared version strings, so
a different executable with the same --version passed. Now a validated ``candidate_digest`` (sha256) is part of the
candidate hash, is REQUIRED before TRIAL_APPROVE/PROMOTE when the changed component is digest-pinned in the reviewed
install-manifest, and is compared at ACCEPTED with the executable digest this domain observed installed."""

from __future__ import annotations

import pytest

from aeos.core.errors import Blocked, ValidationFailed
from aeos.inventory.observations import record_observation
from aeos.models.inventory import ComponentReference
from aeos.proposals import service
from tests.unit.proposal_fixtures import (
    apply_config,
    checkpoint_pass,
    component,
    create,
    decide,
    evidence,
    owner_plan,
    review,
    task,
)
from tests.unit.test_cost_fakes import install_fake_policy

BASELINE = "sha256:" + "1" * 64
CANDIDATE = "sha256:" + "2" * 64
OTHER = "sha256:" + "3" * 64


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _digest_pinned(db, digest: str = BASELINE) -> None:
    comp = component(db)
    comp.details = {**(comp.details or {}), "approved_digest": digest}
    db.commit()


def test_candidate_digest_is_structured_never_free_text(db, policy):
    component(db)
    with pytest.raises(ValidationFailed) as e:
        create(db, kind="UPGRADE", candidate_pin="1.4.0", candidate_digest="the new build")
    assert e.value.code == "CANDIDATE_DIGEST_INVALID"
    db.rollback()
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0", candidate_digest=CANDIDATE.upper().replace("SHA256", "sha256"))
    assert p.candidate_digest == CANDIDATE
    assert service.proposal_view(db, p)["candidate_digest"] == CANDIDATE


def test_a_digest_pinned_baseline_requires_the_candidate_digest_before_trial(db, policy):
    _digest_pinned(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    assert p.review_package["experiment_plan"]["pins"]["candidate_digest"] == "NOT_RECORDED"
    with pytest.raises(Blocked) as e:
        decide(db, p, "TRIAL_APPROVE")
    assert e.value.code == "CANDIDATE_DIGEST_REQUIRED"
    assert e.value.details["digest_pinned_components"] == [{"component_id": "opencode", "approved_digest": BASELINE}]
    db.rollback()
    before = p.candidate_hash
    review(db, p, candidate_digest=CANDIDATE)
    assert p.candidate_hash != before, "the digest is part of what the owner approves"
    assert decide(db, p, "TRIAL_APPROVE").kind == "TRIAL"


def test_without_a_digest_pinned_baseline_the_digest_stays_optional(db, policy):
    component(db)  # no approved_digest recorded (today's reviewed manifest: owner step)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    assert decide(db, p, "TRIAL_APPROVE").kind == "TRIAL"
    assert service.candidate_digest_gate(db, p)["status"] == "NOT_RECORDED"


def _promoted_with_digest(db):
    _digest_pinned(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0", candidate_digest=CANDIDATE)
    owner_plan(db, p)
    review(db, p)
    decide(db, p, "TRIAL_APPROVE")
    t = task(db, p)
    ev = evidence(db, task_spec=t)
    service.attach_evidence(db, p, evidence_ids=[ev.id], actor_user_id="owner")
    db.commit()
    checkpoint_pass(db, p)
    decide(db, p, "PROMOTE")
    return p, t


def _accept(db, p, t):
    ev = evidence(db, task_spec=t)
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration={"component_id": "opencode", "approved_pin": "1.4.0"},
                           acceptance_results={"AC1": {"status": "PASS", "evidence_id": ev.id}})
    db.commit()


def _observe(db, identity: str) -> None:
    record_observation(db, component_id="opencode", dimension="installation", status="INSTALLED",
                       observer="reconciler", observed_version="1.4.0", observed_identity=identity)
    db.commit()


def test_accepted_compares_the_observed_executable_with_the_candidate_digest(db, policy):
    p, t = _promoted_with_digest(db)
    apply_config(db)
    comp = db.get(ComponentReference, "opencode")
    comp.details = {**(comp.details or {}), "approved_digest": CANDIDATE}  # the reviewed manifest now pins it
    db.commit()
    with pytest.raises(Blocked) as e:  # nothing observed yet: the digest cannot be compared
        _accept(db, p, t)
    assert e.value.code == "CONFIG_NOT_APPLIED" and "cannot be compared" in str(e.value.details["problems"])
    db.rollback()
    _observe(db, OTHER)  # same --version, different bytes: not the reviewed candidate
    with pytest.raises(Blocked) as e:
        _accept(db, p, t)
    assert "not the reviewed candidate artifact" in str(e.value.details["problems"])
    db.rollback()
    _observe(db, CANDIDATE)
    _accept(db, p, t)
    assert p.status == "ACCEPTED"


def test_replace_compares_the_digest_with_the_new_component_not_the_retired_one(db, policy):
    """REPLACE: the old engine is digest-pinned, so the candidate must name its digest - and at ACCEPTED that digest
    is the NEW component's executable. The old component keeps its own (different) bytes; comparing it with the
    candidate digest would refuse every replacement."""
    _digest_pinned(db)  # opencode (the component being replaced) is held to BASELINE
    component(db, "opencode-next")
    p = create(db, kind="REPLACE", candidate_pin="1.4.0", candidate_component_ids=["opencode-next"])
    p.links = {"adr_keys": ["ADR-9100"]}  # a REPLACE is an architecture change: ACCEPTED needs its ADR link
    db.commit()
    owner_plan(db, p)
    review(db, p)
    with pytest.raises(Blocked) as e:
        decide(db, p, "TRIAL_APPROVE")
    assert e.value.code == "CANDIDATE_DIGEST_REQUIRED"
    db.rollback()
    review(db, p, candidate_digest=CANDIDATE)
    decide(db, p, "TRIAL_APPROVE")
    t = task(db, p)
    service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t).id], actor_user_id="owner")
    db.commit()
    checkpoint_pass(db, p)
    decide(db, p, "PROMOTE")
    apply_config(db, "opencode-next")
    record_observation(db, component_id="opencode-next", dimension="installation", status="INSTALLED",
                       observer="reconciler", observed_version="1.4.0", observed_identity=CANDIDATE)
    db.commit()
    ev = evidence(db, task_spec=t)
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration={"component_id": "opencode-next", "approved_pin": "1.4.0"},
                           acceptance_results={"AC1": {"status": "PASS", "evidence_id": ev.id}})
    db.commit()
    assert p.status == "ACCEPTED"


def test_one_candidate_digest_names_one_accepted_component(db, policy):
    """A candidate digest is the digest of ONE artifact: with several candidate components the accepted
    configuration must say which component it belongs to (never compared with all of them)."""
    component(db)
    component(db, "opencode-a")
    component(db, "opencode-b")
    p = create(db, kind="ADD", candidate_pin="1.4.0", candidate_digest=CANDIDATE,
               candidate_component_ids=["opencode-a", "opencode-b"])
    problems = service.candidate_digest_problems(db, p, None, ["opencode-a", "opencode-b"])
    assert any("accepted_configuration.component_id" in x for x in problems), problems


def test_a_parts_candidate_digest_is_validated_and_bound_in_the_parent_hash(db, policy):
    from aeos.proposals import review as R

    component(db)
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="ADD", title="Stack", sources=[],
                                parts=[{"title": "Serena", "sources": ["https://github.com/oraios/serena"],
                                        "candidate_pin": "1.0.0", "candidate_digest": "latest"}])
    db.rollback()
    parent = service.create_proposal(
        db, actor_user_id="owner", domain="PERSONAL", kind="ADD", title="Stack", sources=[],
        parts=[{"title": "Serena", "sources": ["https://github.com/oraios/serena"], "candidate_pin": "1.0.0",
                "candidate_digest": CANDIDATE}])
    db.commit()
    child = service._children(db, parent)[0]
    assert child.candidate_digest == CANDIDATE
    cand, with_digest = R.compute_candidate(db, parent, [child])
    assert any(CANDIDATE in part for part in cand["parts"])
    child.candidate_digest = OTHER
    assert R.compute_candidate(db, parent, [child])[1] != with_digest, "a part's digest is part of the approval"


def test_accepted_refuses_a_manifest_digest_other_than_the_candidate(db, policy):
    p, t = _promoted_with_digest(db)
    apply_config(db)  # approved_digest is still the BASELINE one
    _observe(db, CANDIDATE)
    with pytest.raises(Blocked) as e:
        _accept(db, p, t)
    assert "manifest not applied" in str(e.value.details["problems"])
