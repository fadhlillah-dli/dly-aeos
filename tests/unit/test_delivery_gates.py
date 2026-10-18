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
    assert g["G6"]["status"] == "NOT_APPLICABLE" and g["G8"]["status"] == "NOT_APPLICABLE"
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
    w = make_world(db)
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w, changed=["backend/extra.py", "frontend/src/x.tsx"])
    make_review(db, w, report)
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
    assert g["G7"]["status"] == "PASS"
    assert g["G5"]["status"] == "NOT_PROVEN", "no retest on the merged state yet"
    _ci(db, w, "m" * 40, [("backend (ubuntu-latest)", "success")])
    db.commit()
    g = _by(gates.compute_gates(db, w.task.id))
    assert g["G5"]["status"] == "PASS"
    assert g["G6"]["status"] == "NOT_APPLICABLE" and g["G8"]["status"] == "NOT_APPLICABLE"


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
