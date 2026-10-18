"""§15 Security Reviewer (separate session per risk) and §31 high-impact changes ("Perubahan routing/
hooks/IAM/secrets/CI/migration/schema … memakai pemeriksaan dampak tinggi meskipun diff kecil")."""

from __future__ import annotations

import pytest

from aeos.core.errors import Blocked
from aeos.delivery import gates
from aeos.verification import acceptance, review
from aeos.verification.scope import check_scope
from tests.unit.test_verification_support import (
    FakeBridge,
    FakeWorkforce,
    claude_result,
    install_doctor,
    install_planning,
    make_review,
    make_world,
    record_verification,
    review_payload,
)


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def test_scope_flags_high_impact_and_security_sensitive_paths():
    r = check_scope(["backend/aeos/routing/service.py", "backend/aeos/identity/service.py", "docs/a.md"],
                    allowed=["**"], forbidden=[], protected=[])
    d = r.to_dict()
    assert d["impact"] == "HIGH"
    assert "backend/aeos/routing/service.py" in d["high_impact_paths"]
    assert d["security_sensitive_paths"] == ["backend/aeos/identity/service.py"]
    assert check_scope(["backend/aeos/feature/x.py"], allowed=["**"], forbidden=[], protected=[]).to_dict()["impact"] \
        == "NORMAL"


def test_high_impact_change_needs_full_non_regression_coverage(db, wf):
    w = make_world(db)
    report = record_verification(db, w, changed=["backend/aeos/routing/service.py"])
    nr = {"existing_behavior": {"status": "COVERED", "note": ""}, "callers": {"status": "UNKNOWN", "note": ""}}
    make_review(db, w, report, non_regression=nr)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED"
    assert any("non-regression 'callers' is UNKNOWN" in r for r in rec.reasons)
    assert any("non-regression 'integration' is missing" in r for r in rec.reasons)
    make_review(db, w, report)  # all dimensions COVERED
    assert acceptance.evaluate_acceptance(db, w.run.id).acceptance_status == "ACCEPTED"


@pytest.mark.parametrize("variant", ["risk", "policy", "path"])
def test_security_review_required_by_risk_policy_or_sensitive_path(db, wf, variant):
    kwargs = {"risk": {"risk_class": "R3"}, "policy": {}, "path": {}}[variant]
    w = make_world(db, **kwargs)
    if variant == "policy":
        from aeos.models.planning import TaskSpec

        t = TaskSpec(plan_version_id=w.version.id, plan_version_hash=w.version.manifest_hash, project_id=w.project.id,
                     domain="PERSONAL", task_key="sec", title="sec", goal="g", allowed_paths=["**"],
                     spec_hash="sha256:sec", reviewer_policy={"security_review": True})
        db.add(t)
        db.flush()
        w.run.task_id, w.task = t.id, t
        db.commit()
    changed = ["backend/aeos/identity/service.py"] if variant == "path" else ["backend/extra.py"]
    report = record_verification(db, w, changed=changed)
    make_review(db, w, report)
    with pytest.raises(Blocked) as e:
        acceptance.evaluate_acceptance(db, w.run.id)
    assert e.value.code == "SECURITY_REVIEW_REQUIRED"
    make_review(db, w, report, kind="SECURITY", verdict="FAIL")
    assert acceptance.evaluate_acceptance(db, w.run.id).acceptance_status == "REJECTED"
    make_review(db, w, report, kind="SECURITY", verdict="PASS-CONTROLLED")
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "ACCEPTED" and rec.security_review_id


def test_security_review_session_is_separate(db, wf, monkeypatch):
    install_doctor(monkeypatch)
    bridge = FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    w = make_world(db, risk_class="R3")
    record_verification(db, w)
    feature = review.review_run(db, w.run.id, actor="t")
    assert feature.acceptance is None, "security review still missing"
    sec = review.review_run(db, w.run.id, actor="t", reviewer="SECURITY")
    db.commit()
    assert sec.review.reviewer_kind == "SECURITY" and "Security Reviewer" in sec.review.reviewer_identity
    assert bridge.requests[1].purpose == "review.security" and "Security Reviewer" in bridge.requests[1].prompt
    assert review.latest_review(db, w.run.id).id == feature.review.id
    assert review.latest_review(db, w.run.id, reviewer="SECURITY").id == sec.review.id
    assert sec.acceptance and sec.acceptance["acceptance_status"] == "ACCEPTED"
    # only the feature review is mirrored on the run's review_verdict field
    assert all(c.get("review_verdict") != "SECURITY" for c in wf.of("update_run"))


def test_g4_waits_for_required_security_review(db, wf, monkeypatch):
    w = make_world(db, risk_class="R3")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    g = {x["gate"]: x for x in gates.compute_gates(db, w.task.id)["gates"]}
    assert g["G4"]["status"] == "PENDING" and "keamanan" in g["G4"]["summary"]
    make_review(db, w, report, kind="SECURITY")
    result = gates.compute_gates(db, w.task.id)
    assert {x["gate"]: x for x in result["gates"]}["G4"]["status"] == "PASS"
    assert result["separate_status"]["security_reviewer"]["required"] is True


def test_invalid_reviewer_role(db, wf):
    from aeos.core.errors import ValidationFailed

    w = make_world(db)
    with pytest.raises(ValidationFailed):
        review.review_run(db, w.run.id, actor="t", reviewer="CEO")
