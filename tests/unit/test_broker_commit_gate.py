"""Broker push gate (owner rule 2026-10-26, AGENTS.md invariant 11): before ANY push every commit of the pushed
range must be authored AND committed by the configured identity of the provider and carry no AI attribution;
otherwise the push is refused with a clear code, audited, and nothing reaches the remote. PR titles/bodies with AI
attribution are refused before any credential is read. Local bare remotes; placeholder addresses only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import git_broker
from aeos.core import config as config_mod
from aeos.core.attribution import ai_identity_problem, find_ai_attribution
from aeos.core.errors import PolicyDenied
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from tests.conftest import TEST_GIT_EMAIL, TEST_GIT_NAME
from tests.unit.test_workforce_support import init_git_repo, make_project

BRANCH = "aeos/task-g-1234abcd"
REPO = "owner/repo"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _commit(cwd: Path, name: str, email: str, message: str, *, committer: tuple[str, str] | None = None) -> str:
    cn, ce = committer or (name, email)
    env_cfg = ["-c", f"user.name={cn}", "-c", f"user.email={ce}", "-c", "commit.gpgsign=false"]
    (cwd / f"f-{len(list(cwd.glob('f-*')))}.txt").write_text(message, encoding="utf-8")
    _git(cwd, "add", "-A")
    subprocess.run(["git", *env_cfg, "commit", "-q", "--author", f"{name} <{email}>", "-m", message], cwd=cwd,
                   check=True, capture_output=True)
    return _git(cwd, "rev-parse", "HEAD")


def set_identities(monkeypatch, **providers: dict | None) -> None:
    """What the owner wrote in aeos.yaml for this test (``commit_identities``)."""
    monkeypatch.setenv("AEOS_COMMIT_IDENTITIES", json.dumps({k: v for k, v in providers.items() if v is not None}))
    config_mod.reset_settings_cache()


OWNER = {"name": TEST_GIT_NAME, "email": TEST_GIT_EMAIL}


@pytest.fixture
def env(db, tmp_path):
    src = tmp_path / "candidate"
    base = init_git_repo(src)
    _git(src, "checkout", "-q", "-b", BRANCH)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    p = make_project(db)
    return {"src": src, "bare": bare, "base": base, "project": p}


def _run(db, env, head: str, *, base: str | None = None) -> WorkerRun:
    run = WorkerRun(project_id=env["project"].id, domain="PERSONAL", execution_status="REVIEW",
                    workspace_ref=str(env["src"]), branch=BRANCH, head_commit=head, base_commit=base)
    db.add(run)
    db.flush()
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="GIT_BRANCH",
                       scope={"repo": REPO, "branch": BRANCH}, ttl_seconds=300, max_requests=2)
    return run


def _push(db, env, run):
    return git_broker.push_task_branch(db, run_id=run.id, worktree=str(env["src"]), branch=BRANCH,
                                       repo_full_name=REPO, remote_url=str(env["bare"]))


def _remote_refs(env) -> str:
    return _git(env["bare"], "for-each-ref", "--format=%(refname)")


def _denied(db, action="broker.git.push_branch") -> AuditEvent:
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.result == "DENIED")
                    .order_by(AuditEvent.seq.desc()))
    assert row is not None
    return row


def test_push_without_a_configured_github_identity_is_refused_before_the_grant(db, env, monkeypatch):
    set_identities(monkeypatch, bitbucket=OWNER)  # the company identity alone does not cover GitHub
    run = _run(db, env, _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL, "candidate"))
    with pytest.raises(PolicyDenied) as e:
        _push(db, env, run)
    assert e.value.code == "COMMIT_IDENTITY_MISSING" and "commit_identities.github" in e.value.message
    assert "@" not in e.value.message  # the remedy names the key, never an address
    assert _denied(db).details["code"] == "COMMIT_IDENTITY_MISSING"
    grant = db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id))
    assert (grant.used_requests or 0) == 0 and _remote_refs(env) == ""


@pytest.mark.parametrize(("author", "committer"), [
    (("Someone Else", "someone@example.com"), None),                      # foreign author and committer
    ((TEST_GIT_NAME, TEST_GIT_EMAIL), ("Someone Else", "someone@example.com")),  # only the committer differs
    (("Someone Else", "someone@example.com"), (TEST_GIT_NAME, TEST_GIT_EMAIL)),  # only the author differs
])
def test_push_with_a_mismatching_author_or_committer_is_refused(db, env, author, committer):
    head = _commit(env["src"], *author, "candidate", committer=committer)
    run = _run(db, env, head)
    with pytest.raises(PolicyDenied) as e:
        _push(db, env, run)
    assert e.value.code == "COMMIT_IDENTITY_MISMATCH"
    assert "someone@example.com" not in e.value.message and "someone@example.com" not in json.dumps(e.value.details)
    assert _remote_refs(env) == ""  # nothing reached the remote
    se = db.scalar(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id))
    assert se.status == "FAILED" and se.commit_sha == head
    row = _denied(db)
    assert row.details["code"] == "COMMIT_IDENTITY_MISMATCH" and row.details["problems"][0]["sha"] == head


ATTRIBUTION_MESSAGES = [
    "feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    "feat: x\n\n\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
    "aeos: candidate\n\nAEOS-Route: worker.test-free",
]


@pytest.mark.parametrize("message", ATTRIBUTION_MESSAGES)
def test_push_with_an_ai_attribution_in_a_commit_message_is_refused(db, env, message):
    run = _run(db, env, _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL, message))
    with pytest.raises(PolicyDenied) as e:
        _push(db, env, run)
    assert e.value.code == "AI_ATTRIBUTION_IN_COMMIT"
    assert _remote_refs(env) == "" and _denied(db).details["code"] == "AI_ATTRIBUTION_IN_COMMIT"


def test_a_worker_commit_below_the_runtime_head_is_checked_too(db, env):
    _commit(env["src"], "Claude", "noreply@anthropic.com", "wip")  # the worker committed inside its sandbox
    head = _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL, "aeos: candidate")
    run = _run(db, env, head, base=env["base"])
    with pytest.raises(PolicyDenied) as e:
        _push(db, env, run)
    assert e.value.code == "AI_ATTRIBUTION_IN_COMMIT" and _remote_refs(env) == ""


def test_the_range_is_what_the_push_would_publish_not_the_runs_base(db, env, tmp_path):
    """The gate checks every commit no branch of the remote reaches yet: history below the run's base that is not on
    the remote would be published by this push and is checked too; history already on the remote (another
    contributor's commits) is not the push's to check."""
    src = tmp_path / "older"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    base = _commit(src, "Earlier Contributor", "earlier@example.com", "init")
    _git(src, "checkout", "-q", "-b", BRANCH)
    head = _commit(src, TEST_GIT_NAME, TEST_GIT_EMAIL, "aeos: candidate")
    env = {**env, "src": src}
    with pytest.raises(PolicyDenied) as e:  # the remote is empty: "init" would reach it with this push
        _push(db, env, _run(db, env, head, base=base))
    assert e.value.code == "COMMIT_IDENTITY_MISMATCH"
    assert [p["sha"] for p in e.value.details["problems"]] == [base]  # the run's own commit is fine
    assert _remote_refs(env) == ""
    _git(src, "push", "-q", str(env["bare"]), "main:refs/heads/main")  # the existing history is on the remote now
    for recorded_base in (base, None):  # the remote decides the range, not the run's recorded base
        res = _push(db, env, _run(db, env, head, base=recorded_base))
        assert res["ok"] and res["status"] == "PUSHED", res
    assert _git(env["bare"], "rev-parse", f"refs/heads/{BRANCH}") == head


def test_an_unreadable_remote_fails_the_push_and_sends_nothing(env, tmp_path):
    """Without the remote's branches the pushed range is unknown: the push FAILS (nothing sent), it is never made
    with an unchecked range."""
    head = _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL, "aeos: candidate")
    work = tmp_path / "broker-work"
    work.mkdir()
    not_a_repository = tmp_path / "not-a-repository"
    not_a_repository.mkdir()
    res = git_broker.push_from_mirror(source_repo=env["src"], branch=BRANCH, remote_url=str(not_a_repository),
                                      token=None, work_root=work, expected_head=head,
                                      commit_policy=git_broker.CommitPolicy(provider="github", email=TEST_GIT_EMAIL))
    assert res["ok"] is False and res["stage"] == "remote_read" and not res.get("refused") and not res.get("unknown")
    assert "pushed range is unknown" in res["error"]


def test_a_matching_identity_pushes(db, env):
    head = _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL.upper(), "aeos: candidate (run 1234abcd)")
    res = _push(db, env, _run(db, env, head, base=env["base"]))
    assert res["ok"] and res["head_sha"] == head  # addresses compare case-insensitively


def test_pr_with_ai_attribution_is_refused_before_any_credential_or_request(db, env, monkeypatch, secrets_memory):
    reads: list[str] = []
    monkeypatch.setattr(secrets_memory, "get", lambda ref, **kw: reads.append(ref) or "x")
    run = _run(db, env, _commit(env["src"], TEST_GIT_NAME, TEST_GIT_EMAIL, "candidate"))
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="PR_CREATE",
                       scope={"repo": REPO, "head": BRANCH, "base": "main"}, ttl_seconds=300)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as e:
            git_broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                           title="[AEOS] login", body="Adds login.\n\nCo-Authored-By: Claude")
        assert not mock.calls
    assert e.value.code == "AI_ATTRIBUTION_IN_TEXT" and e.value.details["field"] == "body"
    assert reads == [] and _denied(db, "broker.github.create_pr").details["code"] == "AI_ATTRIBUTION_IN_TEXT"
    grant = db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id,
                                                    CapabilityGrant.kind == "PR_CREATE"))
    assert (grant.used_requests or 0) == 0
    with pytest.raises(PolicyDenied) as e2:
        git_broker.create_pull_request(db, run_id=run.id, repo_full_name=REPO, head=BRANCH, base="main",
                                       title="Generated with ChatGPT", body="ok")
    assert e2.value.details["field"] == "title"


def test_the_attribution_samples_of_this_file_are_attribution_for_the_canonical_guard():
    """The samples above are only meaningful while the canonical guard (aeos.core.attribution) still calls them
    attribution; an ordinary message and a human identity stay clean."""
    for message in ATTRIBUTION_MESSAGES + ["Generated with ChatGPT"]:
        assert find_ai_attribution(message), message
    assert ai_identity_problem("Claude", "noreply@anthropic.com")
    assert not find_ai_attribution("aeos: candidate")
    assert ai_identity_problem(TEST_GIT_NAME, TEST_GIT_EMAIL) is None
