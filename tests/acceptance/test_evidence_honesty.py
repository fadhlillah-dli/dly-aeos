"""Evidence honesty (AGENTS.md #7, §34, §35). §50 rows: "Worker exit 0 / menulis 'all tests pass'" →
REVIEW/NOT-PROVEN until the verifier runs; "Worker mengubah grader/policy/pipeline di luar kontrak" →
rejected (quarantine); "PR baru mengubah head atau base" → acceptance no longer valid for the new
identity; UNKNOWN/NOT-PROVEN is never shown as pass."""

from __future__ import annotations

import inspect

import pytest

from aeos.verification import facade as verification
from aeos.verification import scope
from tests.acceptance.support import make_records
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance


def test_worker_evidence_is_an_untrusted_claim(db):
    assert "trusted" not in inspect.signature(verification.collect_evidence).parameters
    ev = verification.collect_evidence(db, kind="TEST_REPORT", origin="worker", collector_identity="opencode",
                                       claim="all tests pass", content="128 passed", evidence_level="E4")
    assert ev.trusted is False and ev.evidence_level == "E0"
    for origin in sorted(verification.TRUSTED_COLLECTORS):
        assert origin != "worker" and origin != "human"


def test_worker_exit_zero_yields_no_acceptance_and_no_pass(personal_client: AuthedClient, db):
    rec = make_records(db)
    verification.collect_evidence(db, kind="TEST_REPORT", origin="worker", collector_identity="opencode",
                                  claim="all tests pass", project_id=rec.project.id, run_id=rec.run.id,
                                  task_id=rec.task.id, evidence_level="E2")
    db.commit()
    assert verification.latest_acceptance(db, rec.task.id) is None
    assert verification.acceptance_is_valid(db, rec.task.id) is False
    gates = personal_client.get(f"/api/delivery/gates?task_id={rec.task.id}").json()
    by = {g["gate"]: g["status"] for g in gates["gates"]}
    assert by["G2"] == "NOT_PROVEN" and by["G4"] != "PASS"
    assert gates["ready_for_human_merge"] is False
    sep = gates["separate_status"]
    assert sep["worker_reported"]["status"] == "ALL_TESTS_PASS" and sep["worker_reported"]["trust"] == "UNTRUSTED_CLAIM"
    assert sep["verifier"]["status"] is None and sep["accepted"]["valid"] is False
    assert all(g["status"] != "PASS" for g in gates["gates"] if g["gate"] != "G0")


def test_grader_policy_pipeline_changes_never_pass_scope():
    protected = list(scope.GENERIC_PROTECTED_PATHS) + list(scope.PLATFORM_PROTECTED_PATHS)
    for path in ("tests/acceptance/test_auth_boundary.py", "policy/office.yaml", ".github/workflows/ci.yml",
                 "backend/aeos/verification/verifier.py", "backend/aeos/broker/git_broker.py",
                 "Backend/AEOS/Policy/x.py"):
        outside = scope.check_scope([path], allowed=["backend/aeos/feature/**"], forbidden=[], protected=protected)
        assert outside.status == "FAIL", path
        allowed = scope.check_scope([path], allowed=["**"], forbidden=[], protected=protected)
        assert allowed.status == "QUARANTINED", path
        assert allowed.quarantine_reason


def test_acceptance_validity_is_bound_to_identity(db):
    from aeos.models.evidence import AcceptanceRecord

    rec = make_records(db)
    db.add(AcceptanceRecord(task_id=rec.task.id, run_id=rec.run.id, project_id=rec.project.id,
                            artifact_hash="sha256:art", base_commit="b" * 40, head_commit="a" * 40,
                            config_hash="sha256:cfg", plan_version_hash=rec.task.plan_version_hash,
                            acceptance_status="ACCEPTED", mode="LIVE"))
    db.commit()
    assert verification.acceptance_is_valid(db, rec.task.id, base_commit="b" * 40, config_hash="sha256:cfg",
                                            plan_version_hash=rec.task.plan_version_hash)
    assert not verification.acceptance_is_valid(db, rec.task.id, base_commit="c" * 40)
    assert not verification.acceptance_is_valid(db, rec.task.id, config_hash="sha256:new-grader")
    assert not verification.acceptance_is_valid(db, rec.task.id, plan_version_hash="sha256:new-plan")


def test_mock_acceptance_is_never_valid(db):
    from aeos.models.evidence import AcceptanceRecord

    rec = make_records(db, key="mock-project")
    db.add(AcceptanceRecord(task_id=rec.task.id, run_id=rec.run.id, project_id=rec.project.id,
                            artifact_hash="sha256:art", acceptance_status="ACCEPTED", mode="MOCK"))
    db.commit()
    assert verification.acceptance_is_valid(db, rec.task.id) is False
