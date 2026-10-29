"""Self-build through the independent verifier (§09, §19, §21, §34; ADR-0052/0054/0060). §50 rows:

* 1.6 "Candidate DLY AEOS mengubah aktif policy/verifier/controller" -> refused at the boundary; the patch
  stays a candidate until a human promotion decision;
* 1.24 "Worker mengubah grader/policy/pipeline di luar kontrak" -> refused before promotion/remote
  publication;
* 1.23 (end to end) "Worker exit 0" -> NOT accepted until the verifier ran and the review passed;
* 2.6 "Worker selesai tetapi verifier gagal" -> failure shown, not accepted;
* 2.13 "Candidate skill meminta auto-update policy" -> refused; goes to review/versioned promotion;
* 2.14 "Controller membangun versi AEOS baru" -> the candidate cannot change the active verifier/suite/
  audit to fake a pass;
* 1.25 "PR baru mengubah head atau base terkait" (acceptance side) -> acceptance STALE, retest needed;
* 1.35 "Deploy sukses tetapi fungsi utama gagal" -> never PASS-PRODUCTION without production evidence.

Everything is real: planning, approval, admission, the worker runtime workspace, the independent
verifier (child pytest of the trusted grader against the candidate snapshot, trusted ruff), the
checkpoint review package and the acceptance engine. In these rows the native bridge is not proven (no
Doctor capability test ran), so the review result arrives through the official manual-package fallback
(§12); ``test_passing_native_capability_test_makes_max_review_ready`` proves the native path's gate.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.errors import PolicyDenied
from aeos.models.audit import AuditEvent
from aeos.models.evidence import AcceptanceRecord
from aeos.models.workforce import TaskExecution, WorkerRun
from tests.acceptance.selfbuild import (
    GOOD_CHANGE,
    manual_review,
    review,
    review_output,
    review_ready,
    self_build,
    verify,
)
from tests.acceptance.world import caps_by_name, task
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance


def _te(db, task_id: str) -> TaskExecution:
    db.expire_all()
    return db.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))


def _merge_accepted_change(monkeypatch, ws) -> None:
    """The owner merges the task's pull request and the checkout is updated: the run's branch is merged into the
    repository the workspace was created from, which is the source checkout the dependency gate reads (L32)."""
    import subprocess

    from aeos.worker_runtime import executor

    monkeypatch.setattr(executor, "resolve_repository", lambda session, project: (ws.source_repo, True))
    identity = ["-c", "user.email=owner@example.invalid", "-c", "user.name=owner"]
    subprocess.run(["git", "-C", str(ws.source_repo), "fetch", "-q", str(ws.git_dir), ws.branch], check=True,
                   capture_output=True)
    subprocess.run(["git", *identity, "-C", str(ws.source_repo), "merge", "-q", "--no-ff", "FETCH_HEAD", "-m",
                    "Merge pull request"], check=True, capture_output=True)


def _row(client: AuthedClient, run_id: str) -> dict:
    return next(r for r in client.get("/api/runs").json()["items"] if r["run_id"] == run_id)


def _acceptance(db, task_id: str) -> AcceptanceRecord | None:
    db.expire_all()
    return db.scalar(select(AcceptanceRecord).where(AcceptanceRecord.task_id == task_id)
                     .order_by(AcceptanceRecord.created_at.desc()))


# ---------------------------------------------------------------------------------------------- 1.23
def test_clean_candidate_is_accepted_only_after_verifier_and_review(personal_client: AuthedClient, db, monkeypatch,
                                                                    tmp_path, host, settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings)
    assert _te(db, w["task_id"]).state == "REVIEW" and _acceptance(db, w["task_id"]) is None
    assert personal_client.post(f"/api/runs/{w['run_id']}/acceptance/evaluate").status_code in (409, 422)
    out = verify(personal_client, w["run_id"])
    assert out["status"] == "PASS" and out["checks"]["trusted_suite"]["status"] == "PASS"
    assert out["checks"]["trusted_suite_integrity"]["status"] == "PASS"
    assert all(c["counted"] is False for c in out["worker_claims"]), "worker claims are listed, never counted"
    assert _te(db, w["task_id"]).state == "REVIEW", "a verifier PASS alone is not acceptance"
    reviewed = manual_review(personal_client, w["run_id"])
    acc = reviewed["acceptance"]
    assert acc["acceptance_status"] == "ACCEPTED" and acc["valid"] is True and acc["is_human_approval"] is False
    assert _te(db, w["task_id"]).state == "DONE"
    assert db.get(WorkerRun, w["run_id"]).merged_status in (None, "NOT_MERGED"), "merge stays a human action"


# ---------------------------------------------------------------------------------------------- 1.6 / 2.13
def test_candidate_changing_active_policy_or_verifier_is_quarantined_until_human_review(
        personal_client: AuthedClient, db, monkeypatch, tmp_path, host, settings, vault):
    from aeos.broker import facade as broker
    from aeos.broker import git_broker

    changes = {**GOOD_CHANGE, "backend/aeos/verification/verifier.py": "PASS_EVERYTHING = True\n",
               "policy/office.yaml": "external_worker_allowed: true\n"}
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings, changes=changes, allowed=["**"])
    out = verify(personal_client, w["run_id"])
    assert out["status"] == "QUARANTINED", out["reasons"]
    assert out["quarantine_reason"] and "protected" in out["quarantine_reason"].lower()
    te = _te(db, w["task_id"])
    assert te.state == "BLOCKED" and "QUARANTINED" in te.reason
    blocked = personal_client.post(f"/api/runs/{w['run_id']}/review", json={"reviewer": "FEATURE"})
    assert blocked.status_code >= 400 and blocked.json()["error"]["code"] == "VERIFICATION_NOT_PASSED", blocked.text
    assert blocked.json()["error"]["details"]["verifier_status"] == "QUARANTINED"
    assert _acceptance(db, w["task_id"]) is None
    # the active controller is untouched: its grader and verifier are what ran
    assert (w["controller"] / "tests" / "acceptance" / "test_grader.py").read_text(encoding="utf-8").startswith("def ")
    run = db.get(WorkerRun, w["run_id"])
    assert run.quarantined is True
    # the patch stays a candidate: the broker refuses remote publication until a human special-review release
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="GIT_BRANCH",
                       scope={"repo": "fadhlillah-dli/dly-aeos", "branch": run.branch}, ttl_seconds=600)
    db.commit()
    with pytest.raises(PolicyDenied) as exc:
        git_broker.push_task_branch(db, run_id=run.id, worktree=run.workspace_ref, branch=run.branch,
                                    repo_full_name="fadhlillah-dli/dly-aeos", remote_url=str(tmp_path / "remote.git"))
    assert exc.value.code == "QUARANTINED", exc.value.code
    db.rollback()


# ---------------------------------------------------------------------------------------------- 1.24 / 2.14
def test_candidate_rewriting_grader_or_pipeline_cannot_fake_its_pass(personal_client: AuthedClient, db, monkeypatch,
                                                                     tmp_path, host, settings, vault):
    from aeos.worker_runtime.scope import PLATFORM_PROTECTED_PATHS, ChangedPath, check_scope

    changes = {"backend/aeos/feature/calc.py": "def add(a, b):\n    return a - b\n",  # wrong
               "tests/acceptance/test_grader.py": "def test_add():\n    assert True\n",  # its own "grader"
               ".github/workflows/ci.yml": "on: [push]\njobs: {}\n"}
    # first line: the runtime's own scope check fails and quarantines this attempt (SCOPE_VIOLATION)
    first = check_scope([ChangedPath(path=p, status="A") for p in changes], allowed=["backend/aeos/feature/**"],
                        forbidden=[], protected=PLATFORM_PROTECTED_PATHS)
    assert first.quarantine and {v["path"] for v in first.violations} >= {"tests/acceptance/test_grader.py",
                                                                         ".github/workflows/ci.yml"}
    # second line, proven on its own: even with the runtime check bypassed the verifier is not fooled
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings, changes=changes,
                   bypass_runtime_scope=True)
    out = verify(personal_client, w["run_id"])
    assert out["status"] == "FAIL"
    assert any("test_add" in t for t in out["failing_tests"]), "the controller's grader ran, not the candidate's"
    assert any("outside" in r or "protected" in r or "forbidden" in r for r in out["reasons"]), out["reasons"]
    assert out["checks"]["trusted_suite_integrity"]["status"] == "PASS"
    assert _acceptance(db, w["task_id"]) is None
    # the repair attempt ships its own test-runner module to hijack the run: refused before the suite runs
    shadow = _repair_attempt(personal_client, db, settings, w, {
        "backend/aeos/feature/calc.py": "def add(a, b):\n    return a + b\n",
        "backend/pytest.py": "def main(*a, **k):\n    return 0\n"})
    assert shadow["status"] == "FAIL" and shadow["checks"]["harness_shadow"]["status"] == "FAIL"
    assert "trusted_suite" not in shadow["checks"] or shadow["checks"]["trusted_suite"]["status"] != "PASS"
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "verification.run")).all()
    assert len(audit) >= 2 and all(a.result == "SUCCESS" for a in audit)  # recorded by the verifier, not faked


def _repair_attempt(client: AuthedClient, db, settings, w: dict, files: dict[str, str]) -> dict:
    """The next (repair) attempt of the same task, with the worker's files written by the runtime."""
    from aeos.core.enums import Domain
    from aeos.worker_runtime import workspace as wsmod
    from aeos.workforce import facade as workforce
    from aeos.workforce import scheduler
    from tests.acceptance.world import admit

    leased = admit(db)
    assert leased, "the repair attempt is admitted"
    run = db.get(WorkerRun, leased[0])
    ws = wsmod.create_workspace(settings.layout, domain=Domain(run.domain), project_key=w["project"]["project_key"],
                                run_id=run.id, task_key=run.task_key, source_repo=w["workspace"].source_repo,
                                base_commit=None, isolated=True)
    for rel, text in files.items():
        (ws.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.repo / rel).write_text(text, encoding="utf-8", newline="\n")
    wsmod.collect_changes(ws)
    head = wsmod.commit_candidate(ws, "repair attempt")
    run.workspace_ref, run.git_dir_ref, run.base_commit, run.head_commit, run.branch = (
        str(ws.repo), str(ws.git_dir), ws.base_commit, head, ws.branch)
    db.commit()
    workforce.transition_task(db, run.task_id, "RUNNING", reason="bound", actor="service:worker_runtime")
    scheduler.set_run_status(db, db.get(WorkerRun, run.id), "RUNNING", reason="bound", actor="service:worker_runtime")
    workforce.transition_task(db, run.task_id, "REVIEW", reason="exit 0", actor="service:worker_runtime")
    scheduler.set_run_status(db, db.get(WorkerRun, run.id), "REVIEW", reason="exit 0", actor="service:worker_runtime")
    db.commit()
    return verify(client, run.id)


# ---------------------------------------------------------------------------------------------- 2.6
def test_worker_done_but_verifier_failed_is_a_visible_failure(personal_client: AuthedClient, db, monkeypatch,
                                                              tmp_path, host, settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings,
                   changes={"backend/aeos/feature/calc.py": "def add(a, b):\n    return a - b\n"})
    out = verify(personal_client, w["run_id"])
    assert out["status"] == "FAIL"
    summary = personal_client.get("/api/verification/summary").json()["items"]
    item = next(i for i in summary if i["run_id"] == w["run_id"])
    assert item["verifier"]["status"] == "FAIL" and item["accepted"]["valid"] is False
    assert item["worker_reported"]["trust"] == "UNTRUSTED_CLAIM"
    row = _row(personal_client, w["run_id"])
    assert row["execution_state"] == "FAILED" and row["ui_state"] == "FAILED"
    assert row["statuses"]["acceptance"] != "ACCEPTED"
    te = _te(db, w["task_id"])
    assert te.state in ("READY", "BLOCKED") and te.pending_findings, "a repair attempt carries the findings"
    gates = personal_client.get(f"/api/delivery/gates?task_id={w['task_id']}").json()
    assert {g["gate"]: g["status"] for g in gates["gates"]}["G2"] in ("FAIL", "NOT_PROVEN")
    assert gates["ready_for_human_merge"] is False


# formerly strict xfail BUG_VERIFIER_STATUS (verifier/workforce status vocabulary), fixed by seam W2
def test_workforce_console_shows_the_verifier_result(personal_client: AuthedClient, db, monkeypatch, tmp_path, host,
                                                     settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings,
                   changes={"backend/aeos/feature/calc.py": "def add(a, b):\n    return a - b\n"})
    out = verify(personal_client, w["run_id"])
    assert out["status"] == "FAIL" and not str(out.get("run_update", "")).startswith("REJECTED")
    assert _row(personal_client, w["run_id"])["statuses"]["verifier"] in ("FAIL", "FAILED")


# ---------------------------------------------------------------------------------------------- 1.25
def test_changed_grader_or_moved_head_makes_acceptance_stale(personal_client: AuthedClient, db, monkeypatch,
                                                             tmp_path, host, settings, vault):
    from aeos.verification import facade as verification

    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings,
                   extra_tasks=[task("T2", deps=["T1"], paths=["docs/**"])])
    verify(personal_client, w["run_id"])
    acc = manual_review(personal_client, w["run_id"])["acceptance"]
    assert acc["acceptance_status"] == "ACCEPTED"
    t2 = w["tasks"]["T2"]
    from aeos.core.config import get_settings
    from aeos.workforce.loop import SchedulerLoop

    SchedulerLoop(get_settings()).tick(start_threads=False, admit=False)
    blocked = _te(db, t2)
    # L32: accepted is not merged yet, so T2 would start from a base without T1's change
    assert (blocked.state, blocked.reason_code) == ("BLOCKED", "DEPENDENCY_NOT_IN_BASE"), blocked.reason
    _merge_accepted_change(monkeypatch, w["workspace"])
    SchedulerLoop(get_settings()).tick(start_threads=False, admit=False)
    assert _te(db, t2).state == "READY", "dependency accepted and merged -> unblocked"
    # the relevant base moved (new commits on the integration branch) -> not valid for the new base
    assert verification.acceptance_is_valid(db, w["task_id"], base_commit="f" * 40) is False
    # the grader changed (new trusted suite) -> the accepted identity no longer holds
    grader = w["controller"] / "tests" / "acceptance" / "test_grader.py"
    grader.write_text(grader.read_text(encoding="utf-8") + "\n\ndef test_add_zero():\n    pass\n", encoding="utf-8")
    r = personal_client.post(f"/api/acceptance/{acc['id']}/recheck")
    assert r.status_code == 200, r.text
    rechecked = r.json()
    assert rechecked["acceptance_status"] in ("STALE", "INVALIDATED") and rechecked["valid"] is False
    assert rechecked["staleness"] or rechecked["invalidated_reason"]
    assert verification.acceptance_is_valid(db, w["task_id"]) is False
    gates = personal_client.get(f"/api/delivery/gates?task_id={w['task_id']}").json()
    assert {g["gate"]: g["status"] for g in gates["gates"]}["G2"] != "PASS"


# ---------------------------------------------------------------------------------------------- 1.35
def test_production_claim_without_production_evidence_is_never_pass_production(
        personal_client: AuthedClient, db, monkeypatch, tmp_path, host, settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings)
    verify(personal_client, w["run_id"])
    reviewed = manual_review(personal_client, w["run_id"], structured=review_output("PASS-PRODUCTION"))
    rv = reviewed["review"]
    assert rv["verdict_requested"] == "PASS-PRODUCTION" and rv["verdict"] == "PASS-CONTROLLED"
    assert any("PASS-PRODUCTION" in u for u in rv["unknowns"])
    acc = reviewed["acceptance"]
    assert acc["review_verdict"] == "PASS-CONTROLLED"
    gates = {g["gate"]: g for g in personal_client.get(f"/api/delivery/gates?task_id={w['task_id']}").json()["gates"]}
    assert gates["G8"]["status"] in ("NOT_APPLICABLE", "NOT_PROVEN", "PENDING") and gates["G8"]["status"] != "PASS"
    assert gates["G6"]["status"] != "PASS"
    runbooks = personal_client.get("/api/ops/runbooks").json()["items"]
    assert runbooks, "incident/rollback runbooks exist for the human-authorised response"


def test_post_deploy_failure_triggers_incident_and_rollback(personal_client: AuthedClient, app):
    """1.35 "jalankan incident/rollback sesuai otorisasi" against a registered production target whose HTTP side is
    faked at the network boundary (respx): the deploy "succeeded" (the target serves the commit) but the main
    function fails. The observation is FAIL; AEOS raises the post_release_failure alert, opens the incident with
    the owner's rollback criteria applied and files a decision request in the one queue. AEOS itself only GETs:
    there is no deploy or rollback route - the rollback is the human's, per authorisation (§40)."""
    import re

    import httpx
    import respx

    sha = "d" * 40
    base = "https://toko.example.org"
    project = personal_client.post("/api/projects", json={"domain": "PERSONAL", "name": "Toko Contoh",
                                                          "idea": "Toko online contoh"}).json()["project"]
    r = personal_client.put(f"/api/projects/{project['id']}/integrations", json={
        "provider": "deployment-target", "purpose": "production", "root_id": base})
    assert r.status_code == 200, r.text
    integration = r.json()["integration"]
    r = personal_client.put(f"/api/delivery/targets/{integration['id']}/checks", json={
        "smoke_checks": [{"name": "checkout", "path": "/api/checkout", "kind": "critical_flow"}],
        "rollback_on": ["critical_flow_fail"]})
    assert r.status_code == 200, r.text
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{base}/health").mock(return_value=httpx.Response(200, json={"status": "ok", "commit": sha}))
        mock.get(f"{base}/api/checkout").mock(return_value=httpx.Response(500, json={"error": "payment down"}))
        r = personal_client.post("/api/delivery/targets/observe",
                                 json={"project_id": project["id"], "environment": "production", "sha": sha})
        assert r.status_code == 200, r.text
        assert [c.request.method for c in mock.calls] == ["GET", "GET"], "observe-only"
    obs = r.json()
    assert obs["status"] == "FAIL" and obs["failure_conditions"] == ["critical_flow_fail"]
    post = obs["post_release"]
    assert post["recommendation"]["status"] == "ROLLBACK_RECOMMENDED"
    assert post["recommendation"]["executed_by"] == "HUMAN" and post["recommendation"]["aeos_action"] == "none"
    alerts = personal_client.get("/api/ops/alerts").json()["items"]
    alert = next(a for a in alerts if a["id"] == post["alert_id"])
    assert alert["kind"] == "post_release_failure" and alert["severity"] == "HIGH"
    assert alert["project_id"] == project["id"] and alert["status"] == "OPEN"
    incidents = personal_client.get("/api/ops/incidents").json()["items"]
    incident = next(i for i in incidents if i["id"] == post["incident_id"])
    assert incident["source"] == "alert" and incident["source_ref"] == alert["id"] and incident["owner"]
    timeline = " ".join(e["note"] for e in incident["timeline"])
    assert "ROLLBACK_RECOMMENDED" in timeline and obs["evidence_id"] in incident["evidence_ids"]
    queue = personal_client.get("/api/decisions/queue").json()["items"]
    request = next(i for i in queue if i["kind"] == "DECISION_REQUEST" and i["target"]["id"] == post["decision_request_id"])
    assert request["project_id"] == project["id"]
    assert not [p for p in app.openapi()["paths"] if re.search(r"\b(deploy|rollback)\b", p.lower())
                and p.startswith("/api/delivery")], "AEOS has no deploy or rollback operation for product targets"


@pytest.mark.integration
def test_post_deploy_failure_on_a_live_target():
    pytest.skip("BLOCKED-no-deployment-target: the trigger path is proven above against a faked target; a live "
                "post-release failure and the owner's authorised rollback need a real registered production target "
                "(master §31, §33): aeos delivery observe-target <project_id> --environment production --sha <commit>")


# ---------------------------------------------------------------------------------------------- review seam
# formerly strict xfail BUG_REVIEW_CAPABILITY (capability_test misread the bridge result); fixed by W3
def test_passing_native_capability_test_makes_max_review_ready(personal_client: AuthedClient, db, monkeypatch,
                                                               host, settings, vault):
    from tests.acceptance.fakes import FakeClaude
    from tests.acceptance.selfbuild import SupervisorResponder

    FakeClaude(monkeypatch, SupervisorResponder())
    review_ready(personal_client, host, settings)
    checks = {c["check_id"]: c for c in personal_client.get("/api/doctor/checks").json()["items"]}
    assert checks["claude.capability_test"]["status"] == "VERIFIED"
    assert caps_by_name(personal_client)["CAN_REVIEW_WITH_MAX"]["status"] == "READY"


def test_review_without_proven_bridge_never_passes_automatically(personal_client: AuthedClient, db, monkeypatch,
                                                                 tmp_path, host, settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings)
    verify(personal_client, w["run_id"])
    waiting = review(personal_client, w["run_id"])["review"]
    assert waiting["status"] == "NEEDS_DECISION" and waiting["verdict"] == "NOT-PROVEN"
    assert _te(db, w["task_id"]).state == "REVIEW" and _acceptance(db, w["task_id"]) is None
