"""Claude checkpoint review (§15, §19, §22, §34). §50 rows: "Fable Max quota habis" → review waits,
gate not passed; reviewer verdict alone is never acceptance (U-14); bridge unavailable → manual
package, never an automatic pass; findings → repair as a new attempt."""

from __future__ import annotations

import pytest

from aeos.core.errors import Blocked, Conflict
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact
from aeos.verification import review
from tests.unit.test_verification_support import (
    FakeBridge,
    FakeWorkforce,
    claude_result,
    install_doctor,
    make_world,
    record_verification,
    remove_module,
    review_payload,
)


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


@pytest.fixture
def world(db):
    return make_world(db)


def test_requires_verifier_pass_first(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    with pytest.raises(Blocked) as e:
        review.review_run(db, world.run.id, actor="t")
    assert e.value.code == "VERIFICATION_REQUIRED"
    record_verification(db, world, status="FAIL")
    with pytest.raises(Blocked) as e:
        review.review_run(db, world.run.id, actor="t")
    assert e.value.code == "VERIFICATION_NOT_PASSED"


def test_run_must_be_in_review(db, wf, monkeypatch):
    w = make_world(db, run_state="DONE")
    with pytest.raises(Conflict):
        review.review_run(db, w.run.id, actor="t")


def test_pass_review_records_review_verify_verdict_and_accepts(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    bridge = FakeBridge(result=claude_result(review_payload("PASS-CONTROLLED"))).install(monkeypatch)
    report = record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    db.commit()
    rec = out.review
    assert rec.status == "COMPLETED" and rec.verdict == "PASS-CONTROLLED"
    assert rec.review["outcome"] == "adds mul()" and rec.verify["test_ids"]
    assert set(rec.non_regression) == {k for k, _ in review.NON_REGRESSION_SCOPE}
    assert rec.target_hash == report.environment["artifact_hash"]
    assert rec.model_resolved == "claude-fable-5-1"
    # the checkpoint package is structured (no transcript) and stored as evidence
    req = bridge.requests[0]
    # §12: package inline -> no file tools, so the supervisor cannot read outside the package
    assert req.purpose == "review.checkpoint" and req.allowed_tools == []
    pkg = db.get(EvidenceArtifact, rec.package_evidence_id)
    assert pkg.kind == "REVIEW_PACKAGE"
    from aeos.verification import collector

    package = collector.read_json(pkg)
    assert {"task", "diff_summary", "changed_files", "verifier", "non_regression_scope"} <= set(package)
    assert "transcript" not in package and "heartbeat" not in str(package).lower()
    assert package["verifier"]["status"] == "PASS"
    # review PASS + verifier PASS → acceptance (decided by code, not by the reviewer)
    assert out.acceptance and out.acceptance["acceptance_status"] == "ACCEPTED"
    assert review.review_view(rec)["is_acceptance"] is False
    assert wf.of("mark_accepted")


def test_quota_exhausted_waits_and_opens_nothing(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(None, ok=False, quota=True)).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    db.commit()
    assert out.review.status == "WAITING_QUOTA"
    assert out.review.verdict == "BLOCKED" and "WAITING_QUOTA" in out.review.status_reason
    assert out.acceptance is None
    assert not wf.of("request_repair") and not wf.of("mark_accepted") and not wf.of("transition_task")
    assert db.query(AcceptanceRecord).count() == 0
    assert wf.of("update_run")[-1]["review_verdict"] == "BLOCKED"


def test_bridge_unavailable_gives_manual_package_never_pass(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge().install(monkeypatch)  # provides the manual package writer
    remove_module(monkeypatch, "aeos.claude_bridge.bridge")  # ...but the native bridge is not built
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.status == "NEEDS_DECISION" and out.review.verdict == "NOT-PROVEN"
    assert out.review.package_ref == out.review.id
    assert out.acceptance is None and not wf.of("request_repair")


def test_doctor_capability_not_ready_uses_manual_path(db, world, wf, monkeypatch):
    install_doctor(monkeypatch, status="BLOCKED", reasons=["claude binary missing"])
    bridge = FakeBridge(result=claude_result(review_payload())).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.status == "NEEDS_DECISION"
    assert "claude binary missing" in out.review.status_reason
    assert not bridge.requests, "no bridge call when the capability is not READY"
    assert bridge.packages == [out.review.id]


def test_no_doctor_and_no_manual_writer_still_fails_closed(db, world, wf, monkeypatch):
    remove_module(monkeypatch, "aeos.doctor.capabilities")
    FakeBridge(result=claude_result(review_payload())).install(monkeypatch, manual=False)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.status == "NEEDS_DECISION" and out.review.verdict == "NOT-PROVEN"
    assert out.review.package_evidence_id in out.review.status_reason


def test_fail_verdict_requests_repair_as_new_attempt(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    findings = [{"severity": "HIGH", "title": "missing authz check", "detail": "x", "required_action": "add check"}]
    FakeBridge(result=claude_result(review_payload("FAIL", findings))).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.verdict == "FAIL"
    repair = wf.of("request_repair")
    assert repair and repair[0]["review_record_id"] == out.review.id
    assert repair[0]["findings"][0]["title"] == "missing authz check"
    assert out.review.repair_status == "APPLIED"
    assert out.acceptance is None


def test_invalid_reviewer_output_is_not_proven(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result({"verdict": "PASS-CONTROLLED"})).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.status == "COMPLETED" and out.review.verdict == "NOT-PROVEN"
    assert "schema" in out.review.status_reason
    assert out.acceptance is None


def test_verdict_clamping():
    v, notes = review.clamp_verdict("PASS-PRODUCTION", has_controlled_evidence=True, findings=[])
    assert v == "PASS-CONTROLLED" and notes
    v, _ = review.clamp_verdict("PASS-CONTROLLED", has_controlled_evidence=False, findings=[])
    assert v == "PASS-STATIC"
    v, _ = review.clamp_verdict("PASS-CONTROLLED", has_controlled_evidence=True,
                                findings=[{"severity": "CRITICAL"}])
    assert v == "NOT-PROVEN"
    assert review.clamp_verdict("LGTM", has_controlled_evidence=True, findings=[])[0] == "NOT-PROVEN"


def test_pass_production_claim_is_clamped_in_record(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload("PASS-PRODUCTION"))).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.verdict_requested == "PASS-PRODUCTION"
    assert out.review.verdict == "PASS-CONTROLLED"
    assert any("clamped" in u for u in out.review.unknowns)


def test_model_policy_mismatch_is_not_proven(db, wf, monkeypatch):
    # TaskSpec rows are immutable, so a second TaskSpec carries the reviewer model policy.
    w = make_world(db)
    from aeos.models.planning import TaskSpec

    t = TaskSpec(plan_version_id=w.version.id, plan_version_hash=w.version.manifest_hash, project_id=w.project.id,
                 domain="PERSONAL", task_key="t2", title="t2", goal="g", allowed_paths=["**"], spec_hash="sha256:t2",
                 reviewer_policy={"allowed_models": ["claude-opus"]})
    db.add(t)
    db.flush()
    w.run.task_id = t.id
    w.task = t
    db.commit()
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload(), model="claude-sonnet-x")).install(monkeypatch)
    record_verification(db, w)
    out = review.review_run(db, w.run.id, actor="t")
    assert out.review.verdict == "NOT-PROVEN"
    assert any("not allowed" in u for u in out.review.unknowns)


def test_mock_review_never_accepts(db, world, wf, monkeypatch):
    install_doctor(monkeypatch)
    FakeBridge(result=claude_result(review_payload(), mode="MOCK")).install(monkeypatch)
    record_verification(db, world)
    out = review.review_run(db, world.run.id, actor="t")
    assert out.review.mode == "MOCK"
    assert out.acceptance is not None and out.acceptance["acceptance_status"] == "REJECTED"
    assert not wf.of("mark_accepted")


def test_manual_result_import(db, world, wf, monkeypatch):
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch, manual=False)
    record_verification(db, world)
    waiting = review.review_run(db, world.run.id, actor="t").review
    db.commit()
    out = review.collect_manual_review(db, waiting.id, actor="owner", structured=review_payload())
    db.commit()
    assert out.review.mode == "MANUAL_FALLBACK" and out.review.supersedes_id == waiting.id
    assert out.review.verdict == "PASS-CONTROLLED"
    assert out.acceptance and out.acceptance["acceptance_status"] == "ACCEPTED"
    with pytest.raises(Conflict):
        review.collect_manual_review(db, out.review.id, actor="owner", structured=review_payload())


def test_manual_result_refused_when_candidate_changed(db, world, wf, monkeypatch):
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch)
    record_verification(db, world)
    waiting = review.review_run(db, world.run.id, actor="t").review
    db.commit()
    record_verification(db, world, artifact_hash="sha256:" + "9" * 64, head="9" * 40)
    with pytest.raises(Conflict) as e:
        review.collect_manual_review(db, waiting.id, actor="owner", structured=review_payload())
    assert e.value.code == "REVIEW_PACKAGE_STALE"


def test_manual_result_refused_after_the_run_left_review(db, world, wf, monkeypatch):
    """A late manual result cannot trigger acceptance or a repair request for a finished attempt."""
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch)
    record_verification(db, world)
    waiting = review.review_run(db, world.run.id, actor="t").review
    world.run.execution_status = "FAILED"
    db.commit()
    with pytest.raises(Conflict) as e:
        review.collect_manual_review(db, waiting.id, actor="owner", structured=review_payload(verdict="FAIL"))
    assert e.value.code == "RUN_NOT_IN_REVIEW"
    assert not wf.of("request_repair") and not wf.of("mark_accepted")


def test_manual_result_refused_when_verification_no_longer_passes(db, world, wf, monkeypatch):
    install_doctor(monkeypatch, status="UNKNOWN")
    FakeBridge().install(monkeypatch)
    record_verification(db, world)
    waiting = review.review_run(db, world.run.id, actor="t").review
    db.commit()
    record_verification(db, world, status="FAIL")  # re-verified (same artifact) and failed
    with pytest.raises(Blocked) as e:
        review.collect_manual_review(db, waiting.id, actor="owner", structured=review_payload())
    assert e.value.code == "VERIFICATION_NOT_PASSED"
