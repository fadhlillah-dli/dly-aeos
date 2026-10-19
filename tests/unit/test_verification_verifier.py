"""Independent Verifier (§09, §19, §34) and §50 rows:

* "Worker exit 0 / menulis 'all tests pass'" → REVIEW/NOT-PROVEN until the verifier runs evidence;
* "Worker mengubah grader/policy/pipeline di luar kontrak" → FAIL + quarantine before promotion;
* "Candidate DLY AEOS mengubah aktif policy/verifier/controller" → the trusted suite comes from the
  controller checkout, never from the candidate's own tests/acceptance copy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, suite, verifier
from tests.unit.test_verification_support import (
    BASE_FILES,
    BUGGY_CALC,
    GIT,
    FakeWorkforce,
    bind_candidate,
    fake_secret,
    git,
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
    # §34 E4: the fully passing trusted suite references the current protected suite by hash + test ids
    assert trusted and all(e.evidence_level == "E4" and e.trusted for e in trusted)
    ref = trusted[0].environment["protected_suite"]
    assert ref["suite_hash"] == out.suite_hash and "tests.acceptance.test_calc::test_add" in ref["test_ids"]
    assert ("update_run", {"run_id": world.run.id, "actor": "verifier", "verifier_status": "PASS"}) in wf.calls
    # §20: the runner is not an isolated sandbox; every report says so (never presented as isolated)
    view = verifier.report_view(report)
    assert view["verifier_isolation"] == "WEAK"
    assert any("NOT-PROVEN" in r and "isolation WEAK" in r for r in collector.read_json(report)["residual_risks"])
    assert view["dependency_set_hash"] == verifier.dependency_set()["hash"]


def test_e4_reference_to_a_changed_protected_suite_is_capped_at_e2(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    trusted = next(e for e in db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "TEST_REPORT"))
                   if "Trusted" in e.claim)
    assert trusted.evidence_level == "E4"
    # the grader changes afterwards: the same reference is no longer the current protected suite
    (controller / "tests" / "acceptance" / "test_new.py").write_text("def test_new():\n    assert True\n",
                                                                       encoding="utf-8")
    again = collector.collect_evidence(db, kind="TEST_REPORT", origin="verifier", collector_identity="v",
                                       claim="stale reference", content={"status": "PASS"}, evidence_level="E4",
                                       project_id=world.project.id, environment=dict(trusted.environment))
    assert again.evidence_level == "E2" and out.status == "PASS"


def test_negative_tests_required_for_api_changes(db, settings, controller, wf):
    """§16/§22/§31 G2: an API change under verifier_policy.negative_tests_required needs an added test that
    asserts a refusal/error path; otherwise the verification is NOT_PROVEN (never PASS)."""
    policy = {**FAST, "negative_tests_required": True}
    world = make_world(db, verifier_policy=policy)
    router = "def get_item(i):\n    if i < 0:\n        raise ValueError('bad')\n    return i\n"
    cand, base, head = make_candidate(settings, world, head_files={"backend/api/router.py": router})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "NOT_PROVEN" and out.checks["negative_tests"].status == "NOT_PROVEN"
    assert any("negative tests required" in n for n in out.not_proven)
    world2 = make_world(db, project_key="neg-ok", verifier_policy=policy)
    cand2, base2, head2 = make_candidate(settings, world2, name="run-2", head_files={
        "backend/api/router.py": router,
        "tests/unit/test_router.py": ("import pytest\n\nfrom api.router import get_item\n\n\n"
                                      "def test_negative_id_is_refused():\n    with pytest.raises(ValueError):\n"
                                      "        get_item(-1)\n")})
    bind_candidate(db, world2, cand2, base2, head2)
    out2 = _verify(db, world2)
    assert out2.checks["negative_tests"].status == "PASS", out2.checks["negative_tests"]
    assert out2.status == "PASS", (out2.reasons, out2.not_proven)


def test_dependency_change_records_a_diff_and_is_not_proven_in_isolation(db, settings, controller, wf):
    """§16 supply chain + §09 Zone 2: a changed Python dependency set yields DEPENDENCY_DIFF evidence with
    licences UNKNOWN, and the run is NOT_PROVEN because the suite ran on the controller's dependencies."""
    world = make_world(db, verifier_policy=FAST, allowed_paths=["backend/**", "tests/unit/**", "requirements.lock"])
    base_files = {**BASE_FILES, "requirements.lock": "fastapi==0.115.0\nhttpx==0.27.0\n"}
    cand, base, head = make_candidate(settings, world, base_files=base_files, head_files={
        "requirements.lock": "fastapi==0.116.1\nhttpx==0.27.0\nleftpad==1.0.0\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "NOT_PROVEN" and any("Zone 2" in n for n in out.not_proven)
    ev = db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "DEPENDENCY_DIFF"))
    data = collector.read_json(ev)
    assert data["licenses"] == "UNKNOWN" and data["auto_upgrade"] is False
    assert data["packages"]["added"] == ["leftpad==1.0.0"]
    assert data["packages"]["upgraded"] == ["fastapi==0.115.0 -> fastapi==0.116.1"]


def test_frontend_and_migration_changes_are_stated_not_proven(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST,
                       allowed_paths=["backend/**", "tests/unit/**", "frontend/**"])
    cand, base, head = make_candidate(settings, world, head_files={
        "frontend/src/App.tsx": "export const App = () => null;\n",
        "backend/migrations/versions/0009_x.py": "revision = '0009'\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["frontend_tests"].status == "NOT_PROVEN"
    assert out.checks["migration_rollback"].status == "NOT_PROVEN"
    assert out.status == "PASS"  # visible NOT-PROVEN items, but nothing the trusted suite failed
    report = db.get(EvidenceArtifact, out.report_evidence_id)
    nps = collector.read_json(report)["not_proven"]
    assert any("frontend" in n for n in nps) and any("migration" in n for n in nps)


def test_baseline_on_the_base_commit_classifies_regressions_and_is_cached(db, settings, controller, wf):
    """§34 "baseline set before the change": with verifier_policy.baseline the trusted suite is measured on the
    recorded base commit (once per base + suite hash); a head failure that passed on the base is a regression."""
    world = make_world(db, verifier_policy={**FAST, "baseline": True})
    cand, base, head = make_candidate(settings, world, head_files={"backend/calc.py": BUGGY_CALC})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL" and out.checks["baseline"].status == "FAIL"
    assert set(out.checks["baseline"].details["regressions"]) == {"tests.acceptance.test_calc::test_add",
                                                                  "tests.acceptance.test_calc::test_add_negative"}
    assert any("critical regression" in r for r in out.reasons)
    base_reports = [e for e in db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "TEST_REPORT"))
                    if (e.environment or {}).get("baseline_commit") == base]
    assert len(base_reports) == 1 and collector.read_json(base_reports[0])["status"] == "PASS"
    # a second attempt on the same base reuses the measured baseline (no second baseline run)
    world2 = make_world(db, project_key=world.project.project_key + "-2", verifier_policy={**FAST, "baseline": True})
    world2.project = world.project
    world2.run.project_id = world.project.id
    world2.run.workspace_ref, world2.run.base_commit, world2.run.head_commit = str(cand), base, head
    db.commit()
    out2 = _verify(db, world2)
    assert out2.checks["baseline"].details["cached_evidence_id"] == base_reports[0].id


def test_taskspec_suites_select_candidate_test_dirs(db, settings, controller, wf):
    world = make_world(db, verifier_policy={**FAST, "suites": ["acceptance"]})
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.checks["candidate_tests"].status == "NOT_RUN"
    assert "suites: acceptance" in out.checks["candidate_tests"].summary
    assert out.checks["trusted_suite"].status == "PASS"  # the trusted suite always runs
    assert not any("integration-marked" in n for n in out.not_proven)


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
    assert out.checks["leak_scan"].status == "FAIL"
    # the stored status survives redaction (a key named "secret_*" would be masked to [REDACTED] and a
    # failing secret scan would then be invisible to gates and the PR bundle)
    view = verifier.report_view(db.get(EvidenceArtifact, out.report_evidence_id))
    assert view["leak_scan_status"] == "FAIL"
    for ev in db.scalars(select(EvidenceArtifact)).all():
        data, integrity = collector.read_content(ev)
        assert integrity in ("VERIFIED", "NO_CONTENT")
        if data:
            assert token.encode() not in data


def _commit_bytes(cand: Path, rel: str, data: bytes) -> str:
    target = cand / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "binary")
    return git(cand, "rev-parse", "HEAD")


def test_secret_inside_a_binary_file_fails(db, settings, controller, wf):
    """git diff shows only "Binary files differ" for files with NUL bytes; the verifier scans them too."""
    world = make_world(db, verifier_policy=FAST)
    token = fake_secret()
    cand, base, _ = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    head = _commit_bytes(cand, "backend/blob.bin", b"\x00\x01PK" + token.encode() + b"\x00\xff")
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["leak_scan"].status == "FAIL"
    assert "1 changed binary file" in out.checks["leak_scan"].summary
    for ev in db.scalars(select(EvidenceArtifact)).all():
        data, _ = collector.read_content(ev)
        assert not data or token.encode() not in data


def test_clean_binary_file_is_scanned_and_passes(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, _ = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    head = _commit_bytes(cand, "backend/blob.bin", b"\x00\x01\x02no credentials here\x00")
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "PASS", out.reasons
    assert "1 changed binary file" in out.checks["leak_scan"].summary


@pytest.mark.parametrize("module_file", ["backend/pytest.py", "backend/tests/__init__.py", "backend/json.py"])
def test_candidate_module_shadowing_the_harness_fails(db, settings, controller, wf, module_file):
    """A candidate backend/ module named like the test runner (or the stdlib) would replace the trusted
    runner on PYTHONPATH — the trusted suite is then not run and the verification fails."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={module_file: "X = 1\n"})
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["harness_shadow"].status == "FAIL"
    assert "trusted_suite" not in out.checks
    assert any("HARNESS_TAMPER" in r for r in out.reasons)


def test_shadowed_modules_ignores_platform_packages(tmp_path):
    trusted = tmp_path / "trusted" / "backend"
    cand = tmp_path / "cand" / "backend"
    for root in (trusted, cand):
        (root / "aeos").mkdir(parents=True)
        (root / "aeos" / "__init__.py").write_text("", encoding="utf-8")
    (cand / "newpkg_aeos_test_only").mkdir()
    (cand / "newpkg_aeos_test_only" / "__init__.py").write_text("", encoding="utf-8")
    (cand / "pluggy.py").write_text("", encoding="utf-8")
    assert verifier.shadowed_modules(cand, trusted) == ["pluggy"]


def test_head_not_descending_from_base_is_not_proven(db, settings, controller, wf):
    """base..head would then include the reversal of unrelated commits: the change identity is unproven."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, world, cand, head, base)  # swapped: "head" is an ancestor of "base"
    out = _verify(db, world)
    assert out.status == "NOT_PROVEN"
    assert any("does not descend" in n for n in out.not_proven)
    assert out.checks["trusted_suite"].status == "PASS", "tests passing never overrides the identity gap"


def test_unrecorded_head_uses_the_task_branch_not_the_checkout(db, settings, controller, wf):
    world = make_world(db, verifier_policy=FAST)
    cand, base, head = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    git(cand, "branch", world.run.branch, head)
    # the worker keeps editing its checkout after finishing (HEAD moves; the task branch does not)
    from tests.unit.test_verification_support import write_files

    write_files(cand, {"backend/calc.py": BUGGY_CALC})
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "after the fact")
    bind_candidate(db, world, cand, base, head)
    world.run.head_commit = None
    db.commit()
    out = _verify(db, world)
    assert out.head_commit == head
    assert out.checks["trusted_suite"].status == "PASS"
    assert any(world.run.branch in n for n in out.not_proven)


def test_windows_reserved_name_in_tree_fails_on_every_os(db, settings, controller, wf):
    """A tree entry named like a Windows device (AUX, NUL, COM1…) is refused on every OS, so the verdict
    does not depend on where the verifier runs (and nothing is ever written to a device)."""
    world = make_world(db, verifier_policy=FAST)
    cand, base, _ = make_candidate(settings, world, head_files={"backend/extra.py": "X = 1\n"})
    # plumbing only (nothing is written to the working tree); Git for Windows refuses such index entries
    # by default (core.protectNTFS), a candidate built on Linux/macOS could still contain them
    plumbing = [GIT or "git", "-c", "core.protectNTFS=false", "-c", "user.name=AEOS Test",
                "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-C", str(cand)]
    blob = subprocess.run([*plumbing, "hash-object", "-w", "--stdin"], input=b"X = 1\n",
                          capture_output=True, check=True).stdout.decode().strip()
    subprocess.run([*plumbing, "update-index", "--add", "--cacheinfo", f"100644,{blob},backend/aux.py"],
                   capture_output=True, check=True)
    subprocess.run([*plumbing, "commit", "-q", "-m", "device name"], capture_output=True, check=True)
    head = git(cand, "rev-parse", "HEAD")
    bind_candidate(db, world, cand, base, head)
    out = _verify(db, world)
    assert out.status == "FAIL"
    assert out.checks["snapshot"].status == "FAIL" and "reserved" in out.checks["snapshot"].summary
    assert out.checks["scope"].status == "FAIL"
    assert "trusted_suite" not in out.checks


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
