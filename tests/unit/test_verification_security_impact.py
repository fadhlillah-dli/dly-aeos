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


def _task_with(db, w, key, **fields):
    from aeos.models.planning import TaskSpec

    t = TaskSpec(plan_version_id=w.version.id, plan_version_hash=w.version.manifest_hash, project_id=w.project.id,
                 domain="PERSONAL", task_key=key, title=key, goal="g", allowed_paths=["**"], spec_hash=f"sha256:{key}",
                 **fields)
    db.add(t)
    db.flush()
    w.run.task_id, w.task = t.id, t
    db.commit()
    return t


@pytest.mark.parametrize("variant,kind", [
    ("ui-path", "A11Y"), ("ui-role", "A11Y"), ("data-role", "PRIVACY"), ("confidential", "PRIVACY"),
    ("iac-path", "SRE"), ("sre-role", "SRE"),
])
def test_specialists_activate_when_output_or_risk_requires_them(db, wf, variant, kind):
    """§16/§17: UX/a11y, privacy/data and SRE specialists become active by output, role or data class —
    each a separate review bound to the same artifact, required for acceptance."""
    w = make_world(db)
    fields = {"ui-role": {"role_key": "frontend-engineer"}, "data-role": {"role_key": "data-ai-feature-engineer"},
              "confidential": {"data_classification": "CONFIDENTIAL"}, "sre-role": {"role_key": "sre-operations-assistant"}}
    if variant in fields:
        _task_with(db, w, variant, **fields[variant])
    changed = {"ui-path": ["frontend/src/pages/x.tsx"], "iac-path": ["deploy/main.tf"]}.get(variant, ["backend/extra.py"])
    report = record_verification(db, w, changed=changed)
    make_review(db, w, report)
    assert kind in review.required_reviews(db, w.task, review.report_view(report), report)
    with pytest.raises(Blocked) as e:
        acceptance.evaluate_acceptance(db, w.run.id)
    assert e.value.code == f"{kind}_REVIEW_REQUIRED"
    make_review(db, w, report, kind=kind, verdict="NOT-PROVEN")
    assert acceptance.evaluate_acceptance(db, w.run.id).acceptance_status == "REJECTED"
    make_review(db, w, report, kind=kind)
    assert acceptance.evaluate_acceptance(db, w.run.id).acceptance_status == "ACCEPTED"


def test_ordinary_backend_change_needs_no_specialist(db, wf):
    w = make_world(db)
    report = record_verification(db, w, changed=["backend/extra.py"])
    assert review.required_reviews(db, w.task, review.report_view(report), report) == {}


def test_review_all_requests_each_open_kind_within_the_review_budget(db, wf, monkeypatch):
    """§19: independent read-only reviews (feature + a11y) for one verified package, each its own session,
    bounded by the run's review budget."""
    install_doctor(monkeypatch)
    bridge = FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    w = make_world(db)
    _task_with(db, w, "budget", role_key="frontend-engineer", reviewer_policy={"max_reviews_per_run": 3})
    record_verification(db, w, changed=["frontend/src/pages/x.tsx"])
    need = review.review_required_kinds(db, w.run.id)
    assert set(need["kinds"]) == {"FEATURE", "A11Y"} and need["budget"] == {"max": 3, "used": 0, "remaining": 3}
    outs = review.review_all(db, w.run.id, actor="owner")
    db.commit()
    assert [o.review.reviewer_kind for o in outs] == ["CLAUDE", "A11Y"]
    assert {r.purpose for r in bridge.requests} == {"review.checkpoint", "review.a11y"}
    assert "Accessibility / UX Reviewer" in bridge.requests[1].prompt
    assert outs[-1].acceptance and outs[-1].acceptance["acceptance_status"] == "ACCEPTED"
    assert not any(s["open"] for s in review.review_required_kinds(db, w.run.id)["kinds"].values())
    review.review_run(db, w.run.id, actor="owner")  # third session: still within the budget of 3
    with pytest.raises(Blocked) as e:
        review.review_run(db, w.run.id, actor="owner", reviewer="A11Y")
    assert e.value.code == "REVIEW_BUDGET_EXHAUSTED"
