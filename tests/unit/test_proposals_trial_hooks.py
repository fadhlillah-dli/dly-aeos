"""Consumers of the proposal decisions (§40): trial project selection, release promotion gated by the
PROMOTION binding, and the scheduler's running-attempt stop condition."""

from __future__ import annotations

import pytest

from aeos.core.errors import Blocked
from aeos.models.inventory import ComponentReference
from aeos.models.research import ChangeProposal
from aeos.proposals import trial
from tests.unit.proposal_fixtures import checkpoint_pass, create, decide, trialled
from tests.unit.test_cost_fakes import install_fake_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def test_office_platform_proposal_has_no_trial_project(db):
    p = create(db, kind="ADD", sources=["https://github.com/acme/widget"], domain="OFFICE")
    with pytest.raises(Blocked) as e:
        trial._trial_project(db, p)
    assert e.value.code == "TRIAL_PROJECT_UNAVAILABLE"


def test_release_promotion_with_a_proposal_requires_its_bound_promotion(db, policy, settings, monkeypatch):
    from aeos.operations import release

    monkeypatch.setattr(release, "_verified_prov", lambda s, rid: {"release_id": rid, "artifact_sha256": "x"})
    monkeypatch.setattr(release, "refuse_worker_context", lambda what: None)
    monkeypatch.setattr(release, "_transition", lambda *a, **k: {"status": "SUCCEEDED", "steps": []})
    q, _t, _ev = trialled(db)
    with pytest.raises(Blocked) as e:  # trial only: no PROMOTION decision
        release.promote(settings, "rel-1", session=db, operator_user_id="owner", proposal_id=q.id)
    assert e.value.code == "PROPOSAL_PROMOTION_NOT_BOUND"
    checkpoint_pass(db, q)
    decide(db, q, "PROMOTE")
    report = release.promote(settings, "rel-2", session=db, operator_user_id="owner", proposal_id=q.id)
    assert report["proposal_id"] == q.id
    db.expire_all()
    assert db.get(ChangeProposal, q.id).links["release_ids"] == ["rel-2"]
    db.get(ComponentReference, "opencode").access = {"egress": ["*"]}  # drift outside the promoted change
    db.commit()
    with pytest.raises(Blocked):
        release.promote(settings, "rel-3", session=db, operator_user_id="owner", proposal_id=q.id)


def test_running_trial_attempt_loses_authority_when_the_binding_goes_stale(db, policy):
    p, t, _ev = trialled(db)
    assert trial.authorisation_lost(db, t.id) is None
    db.get(ComponentReference, "opencode").approved_pin = "1.3.9"
    db.commit()
    why = trial.authorisation_lost(db, t.id)
    assert why and "STALE_BINDING" in why
    assert trial.authorisation_lost(db, None) is None  # tasks not bound to a proposal are unaffected
