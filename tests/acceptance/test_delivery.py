"""Delivery boundary (§31, U-09; ADR-0054/0063). §50 rows:

* 1.33 "Agent mencoba direct push main/merge API" -> refused by a boundary that is actually exercised
  (the broker's real git push path against a local bare remote; no merge operation exists anywhere);
  provider-side enforcement on a real GitHub repository is BLOCKED here;
* 1.25 "PR baru mengubah head atau base terkait" (delivery side) -> PR sync makes the acceptance STALE;
  the PR cannot be prepared today (``BUG_DELIVERY``), so that part is a strict xfail.
"""

from __future__ import annotations

import subprocess

import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import AeosError, PolicyDenied
from aeos.models.workforce import WorkerRun
from aeos.policy import facade as policy
from tests.acceptance.selfbuild import manual_review, self_build, verify
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

BUG_DELIVERY = (
    "BUG: execution/delivery seam - delivery.pr_bundle.prepare_pr and broker.git_broker._interlock build the "
    "external-write WriteIntent with decision_id=None (prepare_pr also without capability_expires_at), so "
    "policy.check_external_write always answers DECISION_MISSING/CAPABILITY_MISSING (INTERLOCK_DENIED); and no "
    "component ever issues the GIT_BRANCH/PR_CREATE capability grants that push_task_branch/create_pull_request "
    "require. An ACCEPTED task can never become a PR.")


def _accepted(client, db, monkeypatch, tmp_path, host, settings) -> dict:
    w = self_build(client, db, monkeypatch, tmp_path, host, settings)
    assert verify(client, w["run_id"])["status"] == "PASS"
    assert manual_review(client, w["run_id"])["acceptance"]["acceptance_status"] == "ACCEPTED"
    return w


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


# ---------------------------------------------------------------------------------------------- 1.33
def test_direct_push_to_main_or_merge_is_refused_by_the_broker_boundary(personal_client: AuthedClient, db,
                                                                         monkeypatch, tmp_path, host, settings,
                                                                         vault, app):
    from aeos.broker import facade as broker
    from aeos.broker import git_broker

    w = _accepted(personal_client, db, monkeypatch, tmp_path, host, settings)
    run = db.get(WorkerRun, w["run_id"])
    repo = "fadhlillah-dli/dly-aeos"
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(remote))
    _git(w["workspace"].source_repo, "push", "-q", str(remote), "main:main")
    main_before = _git(remote, "rev-parse", "refs/heads/main")
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="GIT_BRANCH",
                       scope={"repo": repo, "branch": run.branch}, ttl_seconds=600)
    db.commit()
    for target in ("main", "refs/heads/main", "release/main", "aeos/../main"):
        with pytest.raises(PolicyDenied) as exc:
            git_broker.push_task_branch(db, run_id=run.id, worktree=run.workspace_ref, branch=target,
                                        repo_full_name=repo, remote_url=str(remote))
        assert exc.value.code == "BRANCH_NOT_ALLOWED", target
    for refspec in (f"+refs/heads/{run.branch}:refs/heads/{run.branch}", f":refs/heads/{run.branch}",
                    f"refs/heads/{run.branch}:refs/heads/main"):
        with pytest.raises(PolicyDenied):
            git_broker.validate_refspec(refspec)
    pushed = git_broker.push_task_branch(db, run_id=run.id, worktree=run.workspace_ref, branch=run.branch,
                                         repo_full_name=repo, remote_url=str(remote))
    db.commit()
    assert pushed["ok"] is True and pushed["commit"] == run.head_commit
    assert _git(remote, "rev-parse", f"refs/heads/{run.branch}") == run.head_commit
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before, "the default branch is never touched"
    # there is no merge operation anywhere: not in the broker facade, not in the API, not in the interlock
    assert not [n for n in dir(broker) if "merge" in n.lower()]
    paths = app.openapi()["paths"]
    import re

    assert not [p for p in paths if re.search(r"\b(merge|deploy)\b", p.lower())]
    for op in ("merge_pr", "push_default_branch", "force_push", "deploy_production"):
        intent = policy.WriteIntent(actor_user_id=run.requested_by or "", project_id=run.project_id, domain="PERSONAL",
                                    provider="github-repo", operation=op, target={"repo": repo},
                                    approved_plan_version_id=None, approved_hash=None, decision_id=None)
        res = policy.check_external_write(db, intent)
        assert not res.allowed and any(r.startswith("HUMAN_ONLY_OPERATION") or r.startswith("OPERATION_UNKNOWN")
                                       for r in res.reasons), op


@pytest.mark.integration
def test_provider_enforces_human_only_merge_on_the_real_repository():
    pytest.skip("BLOCKED-real-github-enforcement: branch protection/rulesets on the owner's private GitHub repository "
                "are NOT_ENFORCEABLE on GitHub Free (ADR-0063); a real merge-API bypass test needs the owner's "
                "repository and plan entitlement")


# ---------------------------------------------------------------------------------------------- 1.25
@pytest.mark.xfail(strict=True, reason=BUG_DELIVERY)
def test_pr_with_new_commits_makes_the_acceptance_stale(personal_client: AuthedClient, db, monkeypatch, tmp_path,
                                                        host, settings, vault):
    from aeos.broker import git_broker
    from aeos.core.enums import Capability
    from tests.acceptance.world import doctor_ready

    w = _accepted(personal_client, db, monkeypatch, tmp_path, host, settings)
    run = db.get(WorkerRun, w["run_id"])
    vault.put("personal/github", "gh" + "p_" + "D1e2l3i4v5" * 4, actor="owner")
    doctor_ready(db, Capability.CAN_PREPARE_PR)
    assert personal_client.post("/api/decisions", json={
        "kind": "WRITE_ENABLEMENT", "provider": "github-repo", "enabled": True, "rationale": "owner enables PRs",
        "auth_scope": {"provider": "github-repo"}}).status_code == 201
    # the GitHub git remote is external: the broker's mirror push lands on a fake that records the refspec
    pushes: list[dict] = []
    monkeypatch.setattr(git_broker, "push_from_mirror", lambda **kw: pushes.append(kw) or {
        "ok": True, "stage": "push", "commit": run.head_commit, "output": "fake remote"})
    repo = "fadhlillah-dli/dly-aeos"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"https://api.github.com/repos/{repo}/pulls").respond(201, json={
            "number": 7, "html_url": f"https://github.com/{repo}/pull/7", "head": {"sha": run.head_commit},
            "base": {"sha": "b" * 40}})
        r = personal_client.post(f"/api/tasks/{w['task_id']}/prepare-pr", json={})
        assert r.status_code == 200, r.text
        pr = r.json()["pull_request"]
        router.get(f"https://api.github.com/repos/{repo}/pulls/7").respond(200, json={
            "number": 7, "state": "open", "merged": False, "head": {"sha": "9" * 40}, "base": {"sha": "b" * 40},
            "html_url": f"https://github.com/{repo}/pull/7"})
        router.get(url__regex=r"https://api\.github\.com/repos/.+/commits/.+/check-runs.*").respond(
            200, json={"total_count": 0, "check_runs": []})
        synced = personal_client.post(f"/api/delivery/prs/{pr['id']}/sync")
        assert synced.status_code == 200, synced.text
    assert any("STALE" in n for n in synced.json()["notes"])
    from aeos.verification import facade as verification

    assert verification.acceptance_is_valid(db, w["task_id"]) is False
    assert pushes and all(p["branch"].startswith("aeos/") for p in pushes)


def test_unaccepted_task_cannot_be_prepared_as_a_pr(personal_client: AuthedClient, db, monkeypatch, tmp_path, host,
                                                    settings, vault):
    w = self_build(personal_client, db, monkeypatch, tmp_path, host, settings)
    r = personal_client.post(f"/api/tasks/{w['task_id']}/prepare-pr", json={})
    assert r.status_code in (403, 409, 423) and r.json()["error"]["code"] in (
        "ACCEPTANCE_REQUIRED", "NOT_ACCEPTED", "ACCEPTANCE_INVALID", "ACCEPTANCE_NOT_VALID")
    assert db.scalar(select(WorkerRun).where(WorkerRun.merged_status == "MERGED")) is None
    with pytest.raises(AeosError):
        from aeos.broker import facade as broker

        run = db.get(WorkerRun, w["run_id"])
        broker.create_pull_request(db, run_id=run.id, repo_full_name="fadhlillah-dli/dly-aeos", head=run.branch,
                                   base="main", title="x", body="y")
    db.rollback()
