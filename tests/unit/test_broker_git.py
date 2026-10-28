"""Git/PR broker boundary (§31 'Pembatasan broker wajib diuji terhadap direct push, refspec, merge
API, branch deletion, force-push, dan credential escape'; §50 'Agent mencoba direct push main/merge API')."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import git_broker
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.workforce import WorkerRun
from tests.unit.review_gate_support import record_claude_review, record_push
from tests.unit.test_workforce_support import FakeModules, init_git_repo, make_project

BRANCH = "aeos/task-a-1234abcd"
TOKEN = "ghp_" + "t" * 36
# the identity init_git_repo commits with = the github identity the test owner configured (tests/conftest.py)
OWNER_POLICY = git_broker.CommitPolicy(provider="github", email="test@example.invalid")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo_env(db, tmp_path):
    src = tmp_path / "candidate"
    base = init_git_repo(src)
    _git(src, "checkout", "-q", "-b", BRANCH)
    (src / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "candidate")
    head = _git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    p = make_project(db)
    run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="REVIEW", workspace_ref=str(src),
                    branch=BRANCH, head_commit=head, base_commit=base)
    db.add(run)
    db.flush()
    record_claude_review(db, run)  # owner rule R44: the Claude review PASS of this head (ADR-0132)
    return {"src": src, "bare": bare, "run": run, "head": head}


def _grant(db, run, kind="GIT_BRANCH", scope=None, max_requests=1):
    return broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind=kind,
                              scope=scope or {"repo": "owner/repo", "branch": BRANCH}, ttl_seconds=300,
                              max_requests=max_requests)


def test_push_aeos_branch_to_local_bare(db, repo_env):
    _grant(db, repo_env["run"])
    res = git_broker.push_task_branch(db, run_id=repo_env["run"].id, worktree=str(repo_env["src"]), branch=BRANCH,
                                      repo_full_name="owner/repo", remote_url=str(repo_env["bare"]),
                                      expected_head=repo_env["head"])
    assert res["ok"] and res["status"] == "PUSHED" and res["commit"] == repo_env["head"]
    assert _git(repo_env["bare"], "rev-parse", f"refs/heads/{BRANCH}") == repo_env["head"]
    refs = _git(repo_env["bare"], "for-each-ref", "--format=%(refname)")
    assert refs.split() == [f"refs/heads/{BRANCH}"]  # nothing else (no main, no tags)
    se = db.scalar(select(BrokerSideEffect))
    assert se.status == "SUCCEEDED" and se.kind == "PUSH_BRANCH"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.git.push_branch"))
    # one-shot capability: a second push needs a new grant
    with pytest.raises(PolicyDenied):
        git_broker.push_task_branch(db, run_id=repo_env["run"].id, worktree=str(repo_env["src"]), branch=BRANCH,
                                    repo_full_name="owner/repo", remote_url=str(repo_env["bare"]))


@pytest.mark.parametrize("refspec", [
    f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}",          # force
    f":refs/heads/{BRANCH}",                              # deletion
    "refs/tags/v1:refs/tags/v1",                          # tag
    f"refs/heads/{BRANCH}:refs/heads/main",               # into main
    "refs/heads/main:refs/heads/main",                    # direct main
    f"refs/heads/{BRANCH}:refs/heads/{BRANCH} refs/heads/main:refs/heads/main",  # multiple
    f"refs/heads/{BRANCH}:refs/heads/{BRANCH},refs/heads/x:refs/heads/x",
    f"refs/heads/{BRANCH}",                               # implicit dst
    "refs/heads/aeos/a:refs/heads/aeos/b",                # renaming
    "refs/heads/aeos/../main:refs/heads/aeos/../main",    # traversal
])
def test_refspec_rejections(refspec):
    with pytest.raises(PolicyDenied):
        git_broker.validate_refspec(refspec)


def test_valid_refspec():
    assert git_broker.validate_refspec(f"refs/heads/{BRANCH}:refs/heads/{BRANCH}")[1] == f"refs/heads/{BRANCH}"


@pytest.mark.parametrize("branch", ["main", "master", "aeos/main", "feature/x", f"{BRANCH}:refs/heads/main",
                                    "aeos/x..y", "aeos/.hidden", "aeos/x.lock", "aeos/x/", "-aeos/x", "aeos/a b"])
def test_branch_rejections(branch):
    with pytest.raises(PolicyDenied):
        git_broker.validate_branch(branch)


@pytest.mark.parametrize("url", ["https://user:pw@github.com/o/r.git", f"https://{TOKEN}@github.com/o/r.git",
                                 "http://github.com/o/r.git", "git@github.com:o/r.git", "ext::sh -c x",
                                 "--upload-pack=x", "https://github.com/o/r.git?x=1", "ssh://github.com/o/r.git"])
def test_remote_rejections(url):
    with pytest.raises(PolicyDenied):
        git_broker.validate_remote_url(url)


def test_push_rejects_main_branch_no_grant_and_foreign_worktree(db, repo_env, tmp_path):
    run = repo_env["run"]
    with pytest.raises(PolicyDenied):  # no grant
        git_broker.push_task_branch(db, run_id=run.id, worktree=str(repo_env["src"]), branch=BRANCH,
                                    repo_full_name="owner/repo", remote_url=str(repo_env["bare"]))
    _grant(db, run, max_requests=5)
    with pytest.raises(PolicyDenied):
        git_broker.push_task_branch(db, run_id=run.id, worktree=str(repo_env["src"]), branch="main",
                                    repo_full_name="owner/repo", remote_url=str(repo_env["bare"]))
    with pytest.raises(PolicyDenied):
        git_broker.push_task_branch(db, run_id=run.id, worktree=str(tmp_path), branch=BRANCH,
                                    repo_full_name="owner/repo", remote_url=str(repo_env["bare"]))
    with pytest.raises(ValidationFailed):
        git_broker.push_task_branch(db, run_id=run.id, worktree=str(repo_env["src"]), branch=BRANCH,
                                    repo_full_name="../../etc", remote_url=str(repo_env["bare"]))


def test_remote_github_push_requires_interlock(db, repo_env, secrets_memory, absent_modules):
    absent_modules("aeos.policy.interlock")
    """Interlock missing (policy module absent) -> fail closed before any credential is fetched."""
    secrets_memory.put("personal/github", TOKEN, actor="test")
    _grant(db, repo_env["run"])
    with pytest.raises(Blocked) as exc:
        git_broker.push_task_branch(db, run_id=repo_env["run"].id, worktree=str(repo_env["src"]), branch=BRANCH,
                                    repo_full_name="owner/repo")
    assert exc.value.code == "INTERLOCK_UNAVAILABLE"


def test_credential_only_in_child_env_never_in_config(repo_env, tmp_path):
    work = tmp_path / "broker-work"
    work.mkdir()
    res = git_broker.push_from_mirror(source_repo=repo_env["src"], branch=BRANCH, remote_url=str(repo_env["bare"]),
                                      token=TOKEN, work_root=work, commit_policy=OWNER_POLICY)
    assert res["ok"], res
    for cfg in (repo_env["src"] / ".git" / "config", repo_env["bare"] / "config"):
        text = cfg.read_text(encoding="utf-8")
        assert TOKEN not in text and "extraheader" not in text.lower()
    assert not list(work.glob("push-*.git"))  # private mirror removed
    for f in work.rglob("*"):
        if f.is_file():
            assert TOKEN not in f.read_text(encoding="utf-8", errors="ignore")
    env = git_broker.git_auth_env("https://github.com/o/r.git", TOKEN, empty_global=work / "g")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert TOKEN not in env["GIT_CONFIG_VALUE_0"]  # base64 basic header, registered for redaction
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_worktree_hooks_do_not_run_during_push(repo_env, tmp_path):
    """A malicious pre-push hook planted in the candidate repo never runs (private mirror push)."""
    marker = tmp_path / "hook-ran.txt"
    hook = repo_env["src"] / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    work = tmp_path / "w"
    work.mkdir()
    res = git_broker.push_from_mirror(source_repo=repo_env["src"], branch=BRANCH, remote_url=str(repo_env["bare"]),
                                      token=TOKEN, work_root=work, commit_policy=OWNER_POLICY)
    assert res["ok"] and not marker.exists()


def test_no_merge_capability_anywhere_in_broker():
    """§31/U-09: the broker has no function or API call that integrates a PR (human-only)."""
    pkg = Path(git_broker.__file__).parent
    patterns = [re.compile(p, re.IGNORECASE) for p in (
        r"/merge", r"merge\(", r"[\"']merge[\"']", r"merge_pull", r"/merges\b", r"auto[_-]?merge", r"\.merge\b")]
    offenders = []
    for f in pkg.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for p in patterns:
                if p.search(line):
                    offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert offenders == []
    assert not any("merge" in name.lower() for name in dir(git_broker) + dir(broker))


# ----------------------------------------------------------------------------- PR creation
def _pr_env(db, monkeypatch, secrets_memory, repo_env, *, interlock=True):
    fk = FakeModules(interlock_allowed=interlock).install(monkeypatch)
    secrets_memory.put("personal/github", TOKEN, actor="test")
    _grant(db, repo_env["run"], kind="PR_CREATE", scope={"repo": "owner/repo", "head": BRANCH, "base": "main"},
           max_requests=3)
    record_push(db, repo_env["run"], repo="owner/repo")  # the broker pushed this head first (ADR-0132)
    return fk


def test_create_pr_draft_and_token_never_returned(db, monkeypatch, secrets_memory, repo_env):
    _pr_env(db, monkeypatch, secrets_memory, repo_env)
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post("https://api.github.com/repos/owner/repo/pulls").mock(
            return_value=httpx.Response(201, json={"number": 7, "html_url": "https://github.com/owner/repo/pull/7",
                                                   "head": {"sha": repo_env["head"]}}))
        res = git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                             base="main", title="Task A", body="evidence: NOT-PROVEN")
        sent = route.calls[0].request
        import json as _j

        payload = _j.loads(sent.content)
        assert payload["draft"] is True and payload["head"] == BRANCH and payload["base"] == "main"
        assert sent.headers["authorization"] == f"Bearer {TOKEN}"
        assert len(mock.calls) == 1
    assert res["ok"] and res["number"] == "7" and TOKEN not in str(res)
    again = git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                           base="main", title="Task A", body="x")
    assert again["status"] == "EXISTS"  # idempotent: no second POST


def test_create_pr_reconciles_existing_and_timeouts(db, monkeypatch, secrets_memory, repo_env):
    _pr_env(db, monkeypatch, secrets_memory, repo_env)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post("https://api.github.com/repos/owner/repo/pulls").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("https://api.github.com/repos/owner/repo/pulls").mock(
            return_value=httpx.Response(200, json=[{"number": 9, "html_url": "u", "head": {"sha": repo_env["head"]}}]))
        res = git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                             base="main", title="t", body="b")
    assert res["ok"] and res["number"] == "9"


def test_create_pr_timeout_without_pr_is_unknown(db, monkeypatch, secrets_memory, repo_env):
    _pr_env(db, monkeypatch, secrets_memory, repo_env)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post("https://api.github.com/repos/owner/repo/pulls").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("https://api.github.com/repos/owner/repo/pulls").mock(return_value=httpx.Response(200, json=[]))
        res = git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                             base="main", title="t", body="b")
    assert not res["ok"] and res["status"] == "UNKNOWN"


def test_create_pr_guards(db, monkeypatch, secrets_memory, repo_env):
    _pr_env(db, monkeypatch, secrets_memory, repo_env, interlock=False)
    with pytest.raises(PolicyDenied):  # interlock denies
        git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                       base="main", title="t", body="b")
    with pytest.raises(PolicyDenied):
        git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head=BRANCH,
                                       base="aeos/other", title="t", body="b")
    with pytest.raises(PolicyDenied):
        git_broker.create_pull_request(db, run_id=repo_env["run"].id, repo_full_name="owner/repo", head="main",
                                       base="main", title="t", body="b")
