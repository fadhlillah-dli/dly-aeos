"""Acceptance (§13, §14, §34, U-14). §50 rows: "PR baru mengubah head atau base terkait" → acceptance
STALE and re-verification; "Worker selesai tetapi verifier gagal" → never accepted; reviewer PASS
alone is not acceptance."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from aeos.core.errors import Blocked
from aeos.models.planning import TaskSpec
from aeos.verification import acceptance, facade
from tests.unit.test_verification_support import (
    HEAD,
    FakeWorkforce,
    make_review,
    make_world,
    record_verification,
)


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def test_requires_both_verifier_and_review(db, wf):
    w = make_world(db)
    with pytest.raises(Blocked) as e:
        acceptance.evaluate_acceptance(db, w.run.id)
    assert e.value.code == "VERIFICATION_REQUIRED"
    report = record_verification(db, w)
    with pytest.raises(Blocked) as e:
        acceptance.evaluate_acceptance(db, w.run.id)
    assert e.value.code == "REVIEW_REQUIRED"
    make_review(db, w, report, status="WAITING_QUOTA", verdict="BLOCKED")
    with pytest.raises(Blocked) as e:
        acceptance.evaluate_acceptance(db, w.run.id)
    assert e.value.code == "REVIEW_NOT_COMPLETED"
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert rec.acceptance_status == "ACCEPTED" and rec.reasons == []
    assert rec.head_commit == HEAD and rec.artifact_hash == report.environment["artifact_hash"]
    assert rec.plan_version_hash == w.task.plan_version_hash and rec.task_spec_hash == w.task.spec_hash
    assert rec.verification_evidence_id == report.id and report.id in rec.verifier_evidence_refs
    assert wf.of("mark_accepted")[0]["acceptance_id"] == rec.id
    assert facade.latest_acceptance(db, w.task.id).id == rec.id
    assert facade.acceptance_is_valid(db, w.task.id)
    view = acceptance.acceptance_view(db, rec)
    assert view["is_human_approval"] is False
    assert view["valid"] is True and view["staleness"] == []


def test_verifier_fail_with_review_pass_is_rejected(db, wf):
    w = make_world(db)
    report = record_verification(db, w, status="FAIL")
    make_review(db, w, report, verdict="PASS-CONTROLLED")
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED"
    assert any("verifier status is FAIL" in r for r in rec.reasons)
    assert not wf.of("mark_accepted")
    assert not facade.acceptance_is_valid(db, w.task.id)


@pytest.mark.parametrize("verdict", ["NOT-PROVEN", "FAIL", "BLOCKED", "PASS-STATIC"])
def test_insufficient_verdicts_rejected(db, wf, verdict):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report, verdict=verdict)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED"


def test_pass_static_only_for_docs_only_tasks_that_allow_it(db, wf):
    w = make_world(db, evidence_requirements=["PASS-STATIC"])
    report = record_verification(db, w, changed=["docs/guide.md"])
    make_review(db, w, report, verdict="PASS-STATIC")
    assert acceptance.evaluate_acceptance(db, w.run.id).acceptance_status == "ACCEPTED"
    # docs-only change but TaskSpec does not allow static evidence
    w2 = make_world(db, project_key="p2")
    report2 = record_verification(db, w2, changed=["docs/guide.md"])
    make_review(db, w2, report2, verdict="PASS-STATIC")
    assert acceptance.evaluate_acceptance(db, w2.run.id).acceptance_status == "REJECTED"
    # TaskSpec allows static, but the change is not docs-only
    w3 = make_world(db, project_key="p3", risk_class="R0")
    report3 = record_verification(db, w3, changed=["docs/guide.md", "backend/x.py"])
    make_review(db, w3, report3, verdict="PASS-STATIC")
    assert acceptance.evaluate_acceptance(db, w3.run.id).acceptance_status == "REJECTED"


def test_review_bound_to_other_artifact_or_mock_rejected(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report, target_hash="sha256:other")
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED" and any("different artifact" in r for r in rec.reasons)
    make_review(db, w, report, mode="MOCK")
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED" and rec.mode == "MOCK"


def test_mock_verification_never_accepts(db, wf):
    w = make_world(db)
    report = record_verification(db, w, mode="MOCK")
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED" and any("MOCK" in r for r in rec.reasons)


def test_stale_on_head_change_and_identity_checks(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert facade.acceptance_is_valid(db, w.task.id, base_commit=rec.base_commit, config_hash=rec.config_hash,
                                      plan_version_hash=rec.plan_version_hash)
    assert not facade.acceptance_is_valid(db, w.task.id, base_commit="0" * 40)
    assert not facade.acceptance_is_valid(db, w.task.id, config_hash="sha256:new")
    assert not facade.acceptance_is_valid(db, w.task.id, plan_version_hash="sha256:new")
    # the head moved (new PR commits): read-only staleness first, then explicit invalidation
    w.run.head_commit = "9" * 40
    db.commit()
    assert any("run head changed" in s for s in acceptance.staleness(db, rec))
    assert acceptance.acceptance_view(db, rec)["valid"] is False
    changed = acceptance.invalidate(db, task_id=w.task.id, reason="PR head changed", actor="t",
                                    head_commit="9" * 40)
    db.commit()
    assert [c.id for c in changed] == [rec.id]
    assert rec.acceptance_status == "STALE" and "head" in rec.invalidated_reason
    assert not facade.acceptance_is_valid(db, w.task.id)
    assert wf.of("update_run")[-1]["acceptance_status"] == "STALE"


def test_invalidate_ignores_unchanged_identity(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert acceptance.invalidate(db, task_id=w.task.id, reason="same", actor="t", head_commit=rec.head_commit) == []
    assert rec.acceptance_status == "ACCEPTED"


def test_recheck_persists_config_staleness(db, wf, monkeypatch):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert rec.acceptance_status == "ACCEPTED"
    monkeypatch.setattr(acceptance, "current_config_hash", lambda task: "sha256:" + "2" * 64)
    assert any("configuration changed" in s for s in acceptance.staleness(db, rec))
    assert not facade.acceptance_is_valid(db, w.task.id), "unpersisted drift already makes it invalid"
    acceptance.recheck(db, rec.id, actor="t")
    assert rec.acceptance_status == "STALE"


def test_verification_under_an_older_grader_is_not_accepted(db, wf, monkeypatch):
    """§08/§50 config change: a verification made with another grader/config hash cannot be accepted."""
    w = make_world(db)
    report = record_verification(db, w, config_hash="sha256:" + "1" * 64)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED"
    assert any("another grader" in r for r in rec.reasons)
    assert wf.of("mark_accepted") == []


def test_unknown_current_config_is_not_accepted(db, wf, monkeypatch):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    monkeypatch.setattr(acceptance, "current_config_hash", lambda task: None)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED" and any("UNKNOWN" in r for r in rec.reasons)


def test_plan_version_hash_mismatch_is_not_accepted(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    db.execute(text("DROP TRIGGER IF EXISTS trg_plan_versions_no_update"))  # simulate a tampered binding
    db.execute(text("UPDATE plan_versions SET manifest_hash='sha256:other' WHERE id=:i"), {"i": w.version.id})
    db.commit()
    db.expire_all()
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "REJECTED"
    assert any("PlanVersion hash does not match" in r for r in rec.reasons)


def test_new_acceptance_supersedes_old(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    first = acceptance.evaluate_acceptance(db, w.run.id)
    second = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert first.acceptance_status == "INVALIDATED" and second.acceptance_status == "ACCEPTED"


def test_binding_columns_immutable(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE acceptance_records SET head_commit='x' WHERE id=:i"), {"i": rec.id})
        db.commit()
    db.rollback()


def test_workforce_missing_keeps_record_but_reports(db, monkeypatch):
    from tests.unit.test_verification_support import remove_module

    remove_module(monkeypatch, "aeos.workforce.scheduler")
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    rec = acceptance.evaluate_acceptance(db, w.run.id)
    assert rec.acceptance_status == "ACCEPTED"


def test_task_spec_unchanged_by_acceptance(db, wf):
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    assert db.get(TaskSpec, w.task.id).spec_hash == w.task.spec_hash
