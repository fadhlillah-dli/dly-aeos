"""Independent Verifier (§09, §19, §34) and §50 rows:

* "Worker exit 0 / menulis 'all tests pass'" → REVIEW/NOT-PROVEN until the verifier runs evidence;
* "Worker mengubah grader/policy/pipeline di luar kontrak" → FAIL + quarantine before promotion;
* "Candidate DLY AEOS mengubah aktif policy/verifier/controller" → the trusted suite comes from the
  controller checkout, never from the candidate's own tests/acceptance copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, suite, verifier
from tests.unit.test_verification_support import (
    BUGGY_CALC,
    FakeWorkforce,
    bind_candidate,
    fake_secret,
    make_candidate,
    make_controller,
    make_world,
    remove_module,
    requires_git,
)

pytestmark = requires_git

FAST = {"ruff": False}


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


@pytest.fixture
def wf(monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def _verify(db, world, **kw):
    out = verifier.verify_run(db, world.run.id, actor="tester", **kw)
    db.commit()
    return out


def test_clean_change_passes_with_trusted_e2_report(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "def mul(a, b):\n    return a * b\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS", out.reasons
    assert out.checks["trusted_suite"].status == "PASS"
    assert out.checks["candidate_tests"].status == "PASS"
    assert out.suite_hash == suite.trusted_suite_hash(controller)
    report = db.get(EvidenceArtifact, out.report_evidence_id)
    assert report.trusted and report.origin == "verifier" and report.evidence_level == "E2"
    assert report.environment["suite_hash"] == out.suite_hash
    assert report.environment["commit"] == head and report.environment["base_commit"] == base
    assert verifier.report_view(report)["status"] == "PASS"
    test_reports = db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "TEST_REPORT")).all()
    trusted = [e for e in test_reports if collector.evidence_summary(e).get("suite") == "trusted-acceptance"
               or "Trusted" in e.claim]
    assert trusted and all(e.evidence_level == "E2" and e.trusted for e in trusted)
    assert ("update_run", {"run_id": world.run.id, "actor": "verifier", "verifier_status": "PASS"}) in wf.calls


def test_trusted_suite_used_even_when_candidate_rewrites_its_own_copy(db, settings, controller, wf):
    """The candidate replaces its own tests/acceptance with a trivially passing test and breaks add()."""
    world = make_world(db, verifier_policy=FAST,
                       allowed_paths=["backend/**", "tests/unit/**", "tests/acceptance/**"])
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/calc.py": BUGGY_CALC,
        "tests/acceptance/test_calc.py": "def test_add():\n    assert True\n",
        "tests/unit/test_own.py": "def test_own():\n    assert True\n",
    })
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["trusted_suite"].status == "FAIL"
    # failing ids are the TRUSTED suite's tests (the candidate copy has no test_add_negative at all)
    assert "tests.acceptance.test_calc::test_add" in out.failing_tests
    # test_add_negative exists only in the trusted copy; it ran (and failed) against the buggy code
    assert "tests.acceptance.test_calc::test_add_negative" in out.failing_tests
    # the candidate's own (rewritten) tests passed — a lower-trust signal that cannot rescue the verdict
    assert out.checks["candidate_tests"].status == "PASS"
    assert out.quarantine_reason and "tests/acceptance/test_calc.py" in out.quarantine_reason


def test_worker_claimed_pass_never_counts(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    world.run.worker_reported_status = "ALL_TESTS_PASS"
    collector.collect_evidence(db, kind="TEST_REPORT", origin="worker", collector_identity="opencode-worker",
                               claim="all tests pass", content="42 passed", evidence_level="E2",
                               project_id=world.project.id, run_id=world.run.id, task_id=world.task.id)
    cand, base, head = make_candidate(settings, world, head_files={"backend/calc.py": BUGGY_CALC})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.worker_claims and all(c["counted"] is False for c in out.worker_claims)
    assert any(c["claim"] == "all tests pass" and c["level"] == "E0" for c in out.worker_claims)
    assert wf.of("request_repair"), "a failed verification requests a bounded repair attempt"


def test_protected_path_outside_contract_fails_and_quarantines(db, settings, controller, wf):
    world = make_world(db, verifier_policy={"ruff": False, "candidate_tests": False})
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/aeos/verification/verifier.py": "# weaken the grader\n",
        ".github/workflows/ci.yml": "name: noop\n",
        "policy/office.yaml": "external_worker_allowed: true\n",
    })
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["scope"].status == "FAIL"
    assert any(r.startswith("PROTECTED_PATH") for r in out.reasons)
    assert out.quarantine_reason is not None


def test_protected_path_allowed_by_taskspec_is_quarantined_not_passed(db, settings, controller, wf):
    world = make_world(db, verifier_policy={"ruff": False, "candidate_tests": False},
                       allowed_paths=["backend/**", ".github/workflows/**"])
    cand, base, head = make_candidate(settings, world, head_files={".github/workflows/ci.yml": "name: ci\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "QUARANTINED"
    assert wf.of("transition_task") and wf.of("transition_task")[0]["to"] == "BLOCKED"
    assert not wf.of("request_repair")


def test_secret_in_diff_fails_without_persisting_the_value(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    token = fake_secret()
    cand, base, head = make_candidate(settings, world, head_files={"backend/cfg.py": f'TOKEN = "{token}"\n'})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["secret_scan"].status == "FAIL"
    for ev in db.scalars(select(EvidenceArtifact)).all():
        data, integrity = collector.read_content(ev)
        assert integrity in ("VERIFIED", "NO_CONTENT")
        if data:
            assert token.encode() not in data


def test_skipped_trusted_test_is_not_proven(db, settings, tmp_path, monkeypatch, wf):
    root = make_controller(tmp_path / "ctl", extra={
        "tests/acceptance/test_skip.py": "import pytest\n\n\ndef test_maybe():\n    pytest.skip('env')\n"})
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "NOT_PROVEN"
    assert any("skipped" in n for n in out.not_proven)


def test_harness_tamper_file_fails(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={
        "backend/sitecustomize.py": "import os\n", "backend/extra.py": "import _pytest\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert any("HARNESS_TAMPER" in r for r in out.reasons)
    assert out.checks["harness_tamper"].status == "FAIL"


def test_run_must_be_in_review(db, settings, controller, wf):
    world = make_world(db, run_state="RUNNING")
    with pytest.raises(Conflict):
        verifier.verify_run(db, world.run.id, actor="tester")


def test_candidate_outside_task_data_is_blocked(db, settings, controller, wf, tmp_path):
    world = make_world(db, workspace=str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "CANDIDATE_OUTSIDE_TASK_DATA"
    assert not wf.of("request_repair")
    report = db.get(EvidenceArtifact, out.report_evidence_id)
    assert report.evidence_level == "E1"  # nothing was tested


def test_non_platform_project_has_no_trusted_suite(db, settings, controller, wf):
    world = make_world(db, is_platform=False, project_key="other-app")
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "BLOCKED" and out.blocked_code == "NO_TRUSTED_SUITE"


def test_missing_workforce_module_degrades_closed(db, settings, controller, monkeypatch):
    remove_module(monkeypatch, "aeos.workforce.scheduler")
    world = make_world(db, verifier_policy={"ruff": False, "candidate_tests": False})
    cand, base, head = make_candidate(settings, world, head_files={"backend/calc.py": BUGGY_CALC})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.run_update.startswith("UNAVAILABLE")
    assert out.repair.startswith("UNAVAILABLE")


def test_scope_without_allowed_paths_fails_closed(db, settings, controller, wf):
    world = make_world(db, allowed_paths=[], verifier_policy={"ruff": False, "candidate_tests": False})
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert any("OUTSIDE_ALLOWED" in r for r in out.reasons)
