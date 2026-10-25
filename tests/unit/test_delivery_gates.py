"""Delivery gates G0-G8 (§31) and separate status fields (§35). §50 rows: "Worker exit 0 / all tests
pass" → G2 NOT_PROVEN; "Fable Max quota habis" → G4 BLOCKED; "Agent mencoba direct push main/merge
API" → G7 FAIL when GitHub reports a non-human merger; "Deploy sukses tetapi fungsi utama gagal" →
no PASS-PRODUCTION / G8 not claimed."""

from __future__ import annotations

import types

import pytest

from aeos.delivery import gates, provider
from aeos.models.delivery_ext import PullRequestRecord
from aeos.verification import acceptance, collector
from tests.unit.test_verification_support import (
    HEAD,
    FakeWorkforce,
    install_planning,
    make_review,
    make_world,
    record_release_approvers,
    record_verification,
    remove_module,
)


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def _ci(db, world, sha, runs):
    ci = provider.parse_check_runs("o/r", sha, [{"check_runs": [
        {"name": n, "status": "completed", "conclusion": c, "head_sha": sha} for n, c in runs]}])
    return provider.record_ci_evidence(db, world.project, ci, task_id=world.task.id)


def _by(result):
    return {g["gate"]: g for g in result["gates"]}


def test_fresh_task_nothing_is_pass(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    collector.collect_evidence(db, kind="TEST_REPORT", origin="worker", collector_identity="w", claim="all tests pass",
                               evidence_level="E2", project_id=w.project.id, run_id=w.run.id, task_id=w.task.id)
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G0"]["status"] == "PASS"
    assert g["G1"]["status"] == "NOT_PROVEN"
    assert g["G2"]["status"] == "NOT_PROVEN" and "klaim worker tidak dihitung" in g["G2"]["summary"]
    assert g["G3"]["status"] == "NOT_PROVEN"
    assert g["G4"]["status"] == "PENDING"
    assert g["G5"]["status"] == "PENDING" and g["G7"]["status"] == "PENDING"
    # §31: no registered staging target is not "no staging" - NOT_APPLICABLE needs the owner's decision
    assert g["G6"]["status"] == "NOT_PROVEN" and "staging=NONE" in g["G6"]["summary"]
    assert g["G8"]["status"] == "PENDING"
    assert [x["gate"] for x in gates.compute_gates(db, w.task.id)["gates"]] == [f"G{i}" for i in range(9)]


def test_g0_binding_variants(db, wf, monkeypatch):
    w = make_world(db)
    remove_module(monkeypatch, "aeos.planning.service")
    assert _by(gates.compute_gates(db, w.task.id))["G0"]["status"] == "UNKNOWN"
    install_planning(monkeypatch, None)
    assert _by(gates.compute_gates(db, w.task.id))["G0"]["status"] == "BLOCKED"
    install_planning(monkeypatch, types.SimpleNamespace(id="other", manifest_hash="x", version=2, decision_refs=[]))
    assert _by(gates.compute_gates(db, w.task.id))["G0"]["status"] == "FAIL"
    install_planning(monkeypatch, types.SimpleNamespace(id=w.version.id, manifest_hash="sha256:changed", version=1,
                                                         decision_refs=[]))
    assert _by(gates.compute_gates(db, w.task.id))["G0"]["status"] == "FAIL"


def test_full_happy_path_then_human_merge(db, wf, monkeypatch):
    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, changed=["backend/extra.py", "frontend/src/x.tsx"])
    make_review(db, w, report)
    make_review(db, w, report, kind="A11Y")  # §17: a UI change activates the accessibility/UX reviewer
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    _ci(db, w, HEAD, [("backend (ubuntu-latest)", "success"), ("frontend", "success")])
    db.commit()
    r = gates.compute_gates(db, w.task.id)
    g = _by(r)
    assert [g[k]["status"] for k in ("G0", "G1", "G2", "G3", "G4")] == ["PASS"] * 5
    assert r["ready_for_human_merge"] is True and r["merge_authority"] == "HUMAN_ONLY"
    sep = r["separate_status"]
    assert sep["worker_reported"]["status"] == "DONE" and sep["worker_reported"]["trust"] == "UNTRUSTED_CLAIM"
    assert sep["verifier"]["status"] == "PASS" and sep["reviewer"]["verdict"] == "PASS-CONTROLLED"
    assert sep["accepted"]["status"] == "ACCEPTED" and sep["merged"]["status"] is None

    pr = PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                           acceptance_id=acc.id, repo_full_name="o/r", number=3, head_ref="aeos/t1", base_ref="main",
                           accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b", state="OPEN")
    db.add(pr)
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G5"]["status"] == "PENDING" and g["G7"]["status"] == "PENDING"

    pr.state, pr.merged, pr.merged_by_login, pr.merged_by_type = "MERGED", True, "fadhlillah-dli", "User"
    pr.merge_commit_sha = "m" * 40
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    # §31 G7: a human merged, but nobody recorded who is authorised to release -> not proven yet
    assert g["G7"]["status"] == "NOT_PROVEN" and "approver" in g["G7"]["summary"]
    record_release_approvers(db, w.project, ["Fadhlillah-DLI"])
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G7"]["status"] == "PASS"
    assert g["G5"]["status"] == "NOT_PROVEN", "no retest on the merged state yet"
    _ci(db, w, "m" * 40, [("backend (ubuntu-latest)", "success")])
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G5"]["status"] == "NOT_PROVEN", "the merged-state file delta was not observed yet"
    pr.details = {"merge_delta": {"files": ["backend/extra.py", "frontend/src/x.tsx"], "truncated": False}}
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G5"]["status"] == "PASS"
    # merged, but no target registered and no owner "no staging / no production" decision -> never NOT_APPLICABLE
    assert g["G6"]["status"] == "NOT_PROVEN" and g["G8"]["status"] == "NOT_PROVEN"


def test_g7_merge_by_an_unauthorised_human_fails(db, wf, monkeypatch):
    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    record_release_approvers(db, w.project, ["fadhlillah-dli"])
    db.add(PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                             acceptance_id=acc.id, repo_full_name="o/r", number=5, head_ref="aeos/t1", base_ref="main",
                             accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b", state="MERGED",
                             merged=True, merged_by_login="someone-else", merged_by_type="User",
                             merge_commit_sha="m" * 40))
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G7"]["status"] == "FAIL" and "tidak berwenang" in g["G7"]["summary"]


@pytest.mark.parametrize("actor_kind,domain", [("HUMAN_RECORDED", "PERSONAL"), ("AGENT", "PERSONAL"),
                                               ("HUMAN", "OFFICE")])
def test_release_approvers_need_a_human_decision_of_the_projects_domain(db, wf, monkeypatch, actor_kind, domain):
    """G7 approver list = human-only AUTH: an owner-recorded/agent row, or a decision filed under the other
    domain for this project id, never authorises a release approver (fail closed -> NOT_PROVEN)."""
    from aeos.models.decisions import Decision

    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind=actor_kind, target_type="release_approvers",
                    target_id=w.project.id, domain=domain, auth_scope={"github_logins": ["fadhlillah-dli"]},
                    rationale="not a valid approver record"))
    db.add(PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                             acceptance_id=acc.id, repo_full_name="o/r", number=9, head_ref="aeos/t1", base_ref="main",
                             accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b", state="MERGED",
                             merged=True, merged_by_login="fadhlillah-dli", merged_by_type="User",
                             merge_commit_sha="m" * 40))
    db.commit()
    assert gates.release_approvers(db, w.project) == ([], None)
    assert _by(gates.compute_gates(db, w.task.id))["G7"]["status"] == "NOT_PROVEN"


def test_g5_fails_when_the_merge_brings_protected_paths_beyond_the_verified_change(db, wf, monkeypatch):
    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, changed=["backend/extra.py"])
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    pr = PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                           acceptance_id=acc.id, repo_full_name="o/r", number=6, head_ref="aeos/t1", base_ref="main",
                           accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b", state="MERGED",
                           merged=True, merged_by_login="fadhlillah-dli", merged_by_type="User",
                           merge_commit_sha="m" * 40,
                           details={"merge_delta": {"files": ["backend/extra.py", "policy/office.yaml",
                                                              ".github/workflows/ci.yml"]}})
    db.add(pr)
    _ci(db, w, "m" * 40, [("backend (ubuntu-latest)", "success")])
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G5"]["status"] == "FAIL"
    assert set(g["G5"]["reasons"]) == {"policy/office.yaml", ".github/workflows/ci.yml"}


def test_g0_checks_target_repository_classification_and_scope(db, wf, monkeypatch):
    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    assert _by(gates.compute_gates(db, w.task.id))["G0"]["status"] == "PASS"
    # a PR against a repository that is not registered for the project
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.add(PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                             acceptance_id=acc.id, repo_full_name="evil/fork", number=7, head_ref="aeos/t1",
                             base_ref="main", accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b",
                             state="OPEN"))
    db.commit()
    g0 = _by(gates.compute_gates(db, w.task.id))["G0"]
    assert g0["status"] == "FAIL" and any("evil/fork" in r for r in g0["reasons"])
    # classification below the project floor, and an empty scope
    w2 = make_world(db, project_key="cls", allowed_paths=[])
    w2.project.data_classification = "CONFIDENTIAL"
    db.commit()
    install_planning(monkeypatch, w2.version)
    g0 = _by(gates.compute_gates(db, w2.task.id))["G0"]
    assert g0["status"] == "FAIL"
    assert any("lebih rendah" in r for r in g0["reasons"]) and any("allowed_paths" in r for r in g0["reasons"])
    w3 = make_world(db, project_key="unsafe", allowed_paths=["../outside/**"])
    install_planning(monkeypatch, w3.version)
    g0 = _by(gates.compute_gates(db, w3.task.id))["G0"]
    assert g0["status"] == "FAIL" and any("tidak aman" in r for r in g0["reasons"])


def _deploy_target(db, project, purpose, url="https://staging.example.org", config=None):
    from tests.unit.test_deployment_support import deploy_target

    return deploy_target(db, project, purpose, url, config)


def test_g6_and_g8_are_evaluated_once_targets_are_registered(db, wf, monkeypatch):
    """§04/§31: a registered staging/production target makes G6/G8 evaluable from trusted runtime
    observations of the exact commit; G6 PASS also needs the owner's authorisation of staging deploys to that
    exact target ("deploy staging berizin"); the staging observation itself runs only while
    CAN_VALIDATE_STAGING is READY (§08)."""
    import httpx
    import respx

    from aeos.core.errors import Blocked
    from aeos.delivery import deployment
    from tests.unit.test_deployment_support import authorise_staging, make_staging_ready

    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    _deploy_target(db, w.project, "staging")
    _deploy_target(db, w.project, "production", url="https://app.example.org")
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G6"]["status"] == "NOT_PROVEN" and g["G8"]["status"] == "PENDING"
    # the gate is not READY: nothing is requested from the staging target
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("https://staging.example.org/health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "commit": HEAD}))
        with pytest.raises(Blocked) as exc:
            deployment.observe(db, w.project.id, "staging", HEAD, actor="owner")
        assert exc.value.code == "CAPABILITY_NOT_READY" and not route.called
    db.rollback()
    make_staging_ready(db, w.project)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://staging.example.org/health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "commit": HEAD}))
        out = deployment.observe(db, w.project.id, "staging", HEAD, actor="owner")
        assert all(c.request.method == "GET" for c in mock.calls)
    db.commit()
    assert out["status"] == "PASS"
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G6"]["status"] == "NOT_PROVEN" and "staging=AUTHORISED" in g["G6"]["summary"]
    authorise_staging(db, w.project, "https://staging.example.org")
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G6"]["status"] == "PASS" and out["evidence_id"] in g["G6"]["evidence_refs"]
    # another commit on the target is not attributable -> NOT_PROVEN, never PASS
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://staging.example.org/health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "commit": "f" * 40}))
        assert deployment.observe(db, w.project.id, "staging", HEAD, actor="owner")["status"] == "NOT_PROVEN"
    db.commit()
    assert _by(gates.compute_gates(db, w.task.id))["G6"]["status"] == "NOT_PROVEN"
    db.add(PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                             acceptance_id=acc.id, repo_full_name="o/r", number=8, head_ref="aeos/t1", base_ref="main",
                             accepted_head_sha=HEAD, observed_head_sha=HEAD, bundle_hash="sha256:b", state="MERGED",
                             merged=True, merged_by_login="fadhlillah-dli", merged_by_type="User",
                             merge_commit_sha="e" * 40))
    db.commit()
    assert _by(gates.compute_gates(db, w.task.id))["G8"]["status"] == "NOT_PROVEN"
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://app.example.org/health").mock(
            return_value=httpx.Response(503, json={"status": "down", "commit": "e" * 40}))
        prod = deployment.observe(db, w.project.id, "production", "e" * 40, actor="owner")
    db.commit()
    assert prod["status"] == "FAIL" and prod["post_release"]["incident_id"]
    g8 = _by(gates.compute_gates(db, w.task.id))["G8"]
    assert g8["status"] == "FAIL"
    # G8 links the alert, incident and decision request of the post-release failure (rollback stays human)
    for key in ("alert_id", "incident_id", "decision_request_id"):
        assert prod["post_release"][key] in g8["evidence_refs"], key
    assert "AEOS tidak melakukan rollback" in g8["summary"]


def test_office_delivery_is_stated_disabled_by_default(db, wf, monkeypatch):
    """ADR-0111: OFFICE delivery exists (Bitbucket Cloud) but is DISABLED until a company authorisation, an
    enabled bitbucket/code integration, Cloud and a verified repository; the reasons are shown, never a pass."""
    w = make_world(db, domain="OFFICE", is_platform=False, project_key="office-app")
    install_planning(monkeypatch, w.version)
    r = gates.compute_gates(db, w.task.id)
    dp = r["separate_status"]["delivery_provider"]
    assert dp["status"] == "DISABLED" and "Bitbucket" in dp["reason"]
    assert dp["provider"] == "bitbucket" and dp["ci"] == "bitbucket-pipelines"
    assert any("otorisasi perusahaan" in x for x in dp["reasons"]) and "NOT-PROVEN" in dp["live_proof"]


def test_governance_status_is_shown_and_gates_promotion(db, wf, monkeypatch):
    """§32: the handoff result is visible next to the gates; with a configured (non-null) adapter a
    BLOCKED/REJECTED or missing handoff keeps ready_for_human_merge false."""
    from aeos.delivery import governance

    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acceptance.evaluate_acceptance(db, w.run.id)
    _ci(db, w, HEAD, [("backend (ubuntu-latest)", "success")])
    db.commit()
    r = gates.compute_gates(db, w.task.id)
    gov = r["separate_status"]["governance"]
    assert gov["status"] == "NOT_RUN" and gov["adapter_configured"] is False and r["ready_for_human_merge"] is True
    governance.handoff(db, w.task.id, actor="owner")
    db.commit()
    gov = gates.compute_gates(db, w.task.id)["separate_status"]["governance"]
    assert gov["status"] == "NOT_CONFIGURED" and gov["bound_to_accepted_artifact"] is True

    class Strict:
        name, version, contract_version = "strict-harness", "1", governance.CONTRACT_VERSION

        def handoff(self, payload):
            return governance.GovernanceHandoffOutput(contract_version=governance.CONTRACT_VERSION, adapter=self.name,
                                                      adapter_version="1", result="BLOCKED",
                                                      input_hash=payload.digest(), reasons=("owner review",))

    monkeypatch.setattr(governance, "_adapter", Strict())
    r = gates.compute_gates(db, w.task.id)
    assert r["separate_status"]["governance"]["allows_promotion"] is False and r["ready_for_human_merge"] is False
    governance.handoff(db, w.task.id, actor="owner")
    db.commit()
    r = gates.compute_gates(db, w.task.id)
    assert r["separate_status"]["governance"]["status"] == "BLOCKED" and r["ready_for_human_merge"] is False


def test_non_human_merge_fails_g7_and_head_mismatch_fails_g5(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    pr = PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                           acceptance_id=acc.id, repo_full_name="o/r", number=4, head_ref="aeos/t1", base_ref="main",
                           accepted_head_sha=HEAD, observed_head_sha="9" * 40, bundle_hash="sha256:b", state="MERGED",
                           merged=True, merged_by_login="aeos-bot[bot]", merged_by_type="Bot",
                           merge_commit_sha="m" * 40)
    db.add(pr)
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G7"]["status"] == "FAIL"
    assert g["G5"]["status"] == "FAIL"
    assert g["G2"]["status"] == "STALE"


def test_quota_waiting_review_blocks_g4(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report, status="WAITING_QUOTA", verdict="BLOCKED")
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G4"]["status"] == "BLOCKED" and "WAITING_QUOTA" in g["G4"]["summary"]


def test_review_pass_without_verifier_pass_is_not_g4_pass(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, status="FAIL")
    make_review(db, w, report, verdict="PASS-CONTROLLED")
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G2"]["status"] == "FAIL"
    assert g["G4"]["status"] == "NOT_PROVEN"


def test_ci_failure_and_verifier_lint_failure_fail_g1(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    record_verification(db, w, ruff="FAIL")
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G1"]["status"] == "FAIL"
    w2 = make_world(db, project_key="p2")
    record_verification(db, w2)
    _ci(db, w2, HEAD, [("backend (ubuntu-latest)", "failure")])
    db.commit()
    g2 = _by(gates.compute_gates(db, w2.task.id))
    assert g2["G1"]["status"] == "FAIL"
    assert g2["G3"]["status"] == "NOT_APPLICABLE", "no frontend change and no frontend job"


def test_verifier_secret_scan_failure_fails_g1(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    record_verification(db, w, leak="FAIL")
    _ci(db, w, HEAD, [("backend (ubuntu-latest)", "success")])
    db.commit()
    assert _by(gates.compute_gates(db, w.task.id))["G1"]["status"] == "FAIL"


def test_pass_static_counts_only_for_allowed_docs_only_changes(db, wf, monkeypatch):
    """Consistent with acceptance: PASS-STATIC is a G4 pass only for docs-only changes the TaskSpec allows."""
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, changed=["backend/extra.py"])
    make_review(db, w, report, verdict="PASS-STATIC")
    g4 = _by(gates.compute_gates(db, w.task.id))["G4"]
    assert g4["status"] == "NOT_PROVEN" and "PASS-STATIC" in g4["summary"]
    d = make_world(db, project_key="docs", risk_class="R0")
    install_planning(monkeypatch, d.version)
    report = record_verification(db, d, changed=["docs/guide.md"])
    make_review(db, d, report, verdict="PASS-STATIC")
    assert _by(gates.compute_gates(db, d.task.id))["G4"]["status"] == "PASS"


def test_high_impact_change_needs_every_non_regression_dimension_for_g4(db, wf, monkeypatch):
    """§31: routing/IAM/CI/schema/... changes need full non-regression coverage, as acceptance does."""
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, changed=["backend/aeos/api/deps_extra.py"])
    partial = {"existing_behavior": {"status": "COVERED", "note": "ok"},
               "authn_authz": {"status": "UNKNOWN", "note": "?"}}
    make_review(db, w, report, non_regression=partial)
    g4 = _by(gates.compute_gates(db, w.task.id))["G4"]
    assert g4["status"] == "NOT_PROVEN"
    assert any("authn_authz" in r for r in g4["reasons"]) and any("callers" in r for r in g4["reasons"])


def test_g2_is_stale_when_grader_or_config_changed(db, wf, monkeypatch):
    """§50 "Config/model/provider berubah": an old PASS under another grader/config is STALE, not PASS."""
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    record_verification(db, w)
    assert _by(gates.compute_gates(db, w.task.id))["G2"]["status"] == "PASS"
    monkeypatch.setattr(gates, "current_config_hash", lambda task: "sha256:" + "9" * 64)
    g2 = _by(gates.compute_gates(db, w.task.id))["G2"]
    assert g2["status"] == "STALE" and "Konfigurasi" in g2["summary"]
    monkeypatch.setattr(gates, "current_config_hash", lambda task: None)
    assert _by(gates.compute_gates(db, w.task.id))["G2"]["status"] == "STALE"


def test_truncated_check_runs_are_never_a_pass(db, wf, monkeypatch):
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    record_verification(db, w)
    ci = provider.parse_check_runs("o/r", HEAD, [{"total_count": 150, "check_runs": [
        {"name": "backend (ubuntu-latest)", "status": "completed", "conclusion": "success", "head_sha": HEAD}]}])
    assert ci.truncated and ci.overall == "NOT_PROVEN"
    assert ci.gate_status("G1")[0] == "NOT_PROVEN"
    provider.record_ci_evidence(db, w.project, ci, task_id=w.task.id)
    db.commit()
    assert _by(gates.compute_gates(db, w.task.id))["G1"]["status"] == "NOT_PROVEN"
