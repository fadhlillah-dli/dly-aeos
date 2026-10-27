"""Delivery boundary (§31, U-09; ADR-0054/0063). §50 rows:

* 1.33 "Agent mencoba direct push main/merge API" -> refused by a boundary that is actually exercised
  (the broker's real git push path against a local bare remote; no merge operation exists anywhere);
  provider-side enforcement on a real GitHub repository is BLOCKED here;
* 1.25 "PR baru mengubah head atau base terkait" (delivery side) -> PR sync makes the acceptance STALE
  (the PR is prepared through the broker grants and the interlock bound to the owner's plan approval).
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
from tests.acceptance.world import GIT_TIMEOUT
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance


PLATFORM_REPO = "fadhlillah-dli/dly-aeos"
# The owner's plan approval names the repository: a PR write is bound to that APPROVAL (create_pr in its
# allowed operations, the repository in its targets) - the interlock refuses DECISION_MISSING without it.
DELIVERY_TARGETS = [{"provider": "github-repo", "repo": PLATFORM_REPO}]


def _accepted(client, db, monkeypatch, tmp_path, host, settings, *, targets: list[dict] | None = None) -> dict:
    w = self_build(client, db, monkeypatch, tmp_path, host, settings, targets=targets)
    assert verify(client, w["run_id"])["status"] == "PASS"
    assert manual_review(client, w["run_id"])["acceptance"]["acceptance_status"] == "ACCEPTED"
    return w


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, timeout=GIT_TIMEOUT).stdout.strip()


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


LIVE_REPO_ENV = "DLY_AEOS_LIVE_BYPASS_REPO"  # owner/name of the repository whose ruleset is tested
LIVE_BRANCH_ENV = "DLY_AEOS_LIVE_BYPASS_BRANCH"  # dedicated probe branch covered by the same ruleset as main
LIVE_TOKEN_REF_ENV = "DLY_AEOS_LIVE_BYPASS_TOKEN_REF"  # secret-store ref of a low-privilege, NON-bypass token
PROBE_PREFIX = "aeos-probe/"
GITHUB = "https://api.github.com"


def _live_settings() -> tuple[str, str, str]:
    import os

    repo, branch, ref = (os.environ.get(k, "").strip() for k in (LIVE_REPO_ENV, LIVE_BRANCH_ENV, LIVE_TOKEN_REF_ENV))
    if not (repo and branch and ref):
        pytest.skip("BLOCKED-real-github-enforcement: opt-in live test, never run by default. Provider rulesets are "
                    "enforceable only on a public repository or a paid plan (a private repository on GitHub Free is "
                    "NOT_ENFORCEABLE, ADR-0063). With an active ruleset on main whose branch targets also include "
                    f"{PROBE_PREFIX}**, opt in with {LIVE_REPO_ENV}=<owner/name>, {LIVE_BRANCH_ENV}={PROBE_PREFIX}"
                    f"<name> (a dedicated probe branch covered by that same ruleset) and {LIVE_TOKEN_REF_ENV}=<secret "
                    "ref of a low-privilege token without bypass>")
    if not branch.startswith(PROBE_PREFIX) or branch in ("main", "master") or ".." in branch:
        pytest.fail(f"{LIVE_BRANCH_ENV} must name a dedicated probe branch under {PROBE_PREFIX} (never main)")
    return repo, branch, ref


@pytest.mark.integration
@pytest.mark.network
def test_provider_enforces_human_only_merge_on_the_real_repository(tmp_path, request):
    """1.33 provider side, OPT-IN and owner-run only (env vars above plus DLY_AEOS_REAL_SECRETS=1, because the token
    is read from the owner's vault, which no other test can reach; skipped otherwise, deselected by the verifier).

    Safe even if protection is absent: every attempt targets a dedicated probe branch (``aeos-probe/...``, refused
    when it is the default branch or not covered by a ruleset) and a throwaway PR into it - main is never touched.
    With a low-privilege token that has no bypass it attempts (1) a direct push, (2) a force-push, (3) a branch
    deletion and (4) a merge-API call, and expects the provider to refuse each one and the probe branch to be
    unchanged. Recording the provider's own record of the refusal is the product's read-only reader afterwards:
    ``aeos delivery enforcement --repo <repo> --branch <probe branch>`` (PROVIDER_ENFORCEMENT_TEST evidence; then
    ``aeos delivery capabilities --record`` shows enforcement VERIFIED for main while the same ruleset applies)."""
    import base64
    import uuid

    import httpx

    from aeos.core.redaction import redact_text, register_secret
    from aeos.secrets import facade as secrets

    repo, branch, ref = _live_settings()  # BLOCKED-real-github-enforcement first: the opt-in below only lifts the guard
    request.getfixturevalue("real_secret_stores")  # owner opt-in DLY_AEOS_REAL_SECRETS=1: the token is in the vault
    token = secrets.get_secret(ref, purpose=f"{ref.split('/', 1)[0]}:delivery.bypass-probe", actor="live-bypass-test")
    register_secret(token)
    api = httpx.Client(base_url=GITHUB, timeout=20.0, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dly-aeos-live-bypass-test"})
    try:
        info = api.get(f"/repos/{repo}").json()
        if info.get("default_branch") == branch:
            pytest.fail("the probe branch is the default branch; refusing to run")
        rules = api.get(f"/repos/{repo}/rules/branches/{branch}")
        types = {r.get("type") for r in rules.json()} if rules.status_code == 200 else set()
        if not types & {"pull_request", "non_fast_forward", "deletion", "update"}:
            pytest.skip(f"BLOCKED-probe-branch-not-covered: no active ruleset applies to {branch}; nothing attempted")
        base = api.get(f"/repos/{repo}/branches/{branch}").json()["commit"]["sha"]
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",  # the credential never appears in argv
               "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
               "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
        url = f"https://github.com/{repo}.git"
        work = tmp_path / "probe"
        work.mkdir()

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            import os

            # credential.helper= : never fall back to the owner's own stored credential (it may hold a bypass)
            return subprocess.run(["git", "-c", "core.hooksPath=", "-c", "credential.helper=", "-c", "user.name=aeos-probe",
                                   "-c", "user.email=probe@invalid", *args], cwd=work, capture_output=True,
                                  text=True, timeout=GIT_TIMEOUT * 4, env={**os.environ, **env})

        assert git("init", "-q").returncode == 0
        assert git("fetch", "-q", "--depth", "1", url, f"refs/heads/{branch}").returncode == 0
        git("checkout", "-q", "--detach", "FETCH_HEAD")
        git("commit", "-q", "--allow-empty", "-m", "aeos enforcement probe (expected to be refused)")
        attempts: dict[str, str] = {}
        direct = git("push", url, f"HEAD:refs/heads/{branch}")
        attempts["direct_push"] = "REFUSED" if direct.returncode != 0 else "ACCEPTED"
        git("checkout", "-q", "--orphan", "aeos-orphan")
        git("commit", "-q", "--allow-empty", "-m", "aeos force probe (expected to be refused)")
        forced = git("push", url, f"+HEAD:refs/heads/{branch}")
        attempts["force_push"] = "REFUSED" if forced.returncode != 0 else "ACCEPTED"
        deleted = git("push", url, f":refs/heads/{branch}")
        attempts["delete_branch"] = "REFUSED" if deleted.returncode != 0 else "ACCEPTED"
        # a throwaway PR from a scratch branch into the probe branch, then the merge API
        src = f"aeos-probe-src/{uuid.uuid4().hex[:8]}"
        git("checkout", "-q", "--detach", "FETCH_HEAD")
        git("commit", "-q", "--allow-empty", "-m", "aeos merge-api probe")
        if git("push", url, f"HEAD:refs/heads/{src}").returncode != 0:
            pytest.fail("could not push the scratch branch for the merge-API attempt (token needs contents:write on "
                        "non-protected branches)")
        pr = api.post(f"/repos/{repo}/pulls", json={"title": "aeos enforcement probe (do not merge)", "head": src,
                                                    "base": branch, "body": "Created by the opt-in live test."})
        number = pr.json().get("number") if pr.status_code == 201 else None
        try:
            assert number, f"throwaway PR could not be created (HTTP {pr.status_code})"
            merge = api.put(f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": "squash"})
            # 403/405 is the provider refusing the merge; a conflict (409) or a validation error (422, e.g. the
            # merge method is disabled) proves nothing about enforcement and fails the run as inconclusive
            code = merge.status_code
            attempts["merge_api"] = ("REFUSED" if code in (403, 405) else "ACCEPTED" if code == 200
                                     else f"INCONCLUSIVE_HTTP_{code}")
        finally:
            if number:
                api.patch(f"/repos/{repo}/pulls/{number}", json={"state": "closed"})
            api.delete(f"/repos/{repo}/git/refs/heads/{src}")
        after = api.get(f"/repos/{repo}/branches/{branch}")
        unchanged = after.status_code == 200 and after.json()["commit"]["sha"] == base
    finally:
        api.close()
    detail = redact_text(" | ".join(f"{k}={v}" for k, v in attempts.items()))
    assert all(v == "REFUSED" for v in attempts.values()), f"provider did not refuse every bypass: {detail}"
    assert unchanged, "the probe branch changed: provider enforcement is NOT proven"


# ---------------------------------------------------------------------------------------------- 1.25
# formerly strict xfail BUG_DELIVERY (WriteIntent without decision id, no delivery grants), fixed by seam W2
def test_pr_with_new_commits_makes_the_acceptance_stale(personal_client: AuthedClient, db, monkeypatch, tmp_path,
                                                        host, settings, vault):
    from aeos.broker import git_broker
    from aeos.core.enums import Capability
    from tests.acceptance.world import doctor_ready

    w = _accepted(personal_client, db, monkeypatch, tmp_path, host, settings, targets=DELIVERY_TARGETS)
    run = db.get(WorkerRun, w["run_id"])
    vault.put("personal/github", "gh" + "p_" + "D1e2l3i4v5" * 4, actor="owner")
    assert personal_client.post("/api/decisions", json={
        "kind": "WRITE_ENABLEMENT", "provider": "github-repo", "enabled": True, "rationale": "owner enables PRs",
        "auth_scope": {"provider": "github-repo"}}).status_code == 201
    # controlled precondition after the owner decision it depends on (it marks dependent results STALE)
    doctor_ready(db, Capability.CAN_PREPARE_PR)
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
