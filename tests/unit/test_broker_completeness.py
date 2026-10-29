"""Completeness-review fixes for the broker (§14 outbox/reconciliation, §21 quarantine, §26 credential
separation, §31 publication boundary, §50 'Worker mengubah grader/policy/pipeline di luar kontrak ->
Ditolak sebelum promotion/remote publication', 'Provider timeout setelah create resource')."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.api.routers import MODULE_ROUTERS
from aeos.broker import facade as broker
from aeos.broker import git_broker
from aeos.core.errors import PolicyDenied
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.decisions import Decision
from aeos.models.workforce import WorkerRun
from aeos.workforce.integrations import QUARANTINE_TARGET_TYPE
from tests.conftest import make_app
from tests.unit.review_gate_support import record_claude_review
from tests.unit.test_workforce_support import FakeModules, init_git_repo, make_project, make_route

# The real routers (and the services they import at module level) load before the proxy fixture's FakeModules replace
# implementation modules in sys.modules, so this file also runs on its own and in any order (as
# tests/unit/test_broker_model_proxy.py does; otherwise make_app imported aeos.doctor.views against the fake doctor).
for _mod in MODULE_ROUTERS:
    importlib.import_module(_mod)

BRANCH = "aeos/task-a-1234abcd"
TOKEN = "ghp_" + "t" * 36


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(db, tmp_path):
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
                    branch=BRANCH, head_commit=head, base_commit=base, git_dir_ref=str(src / ".git"))
    db.add(run)
    db.flush()
    record_claude_review(db, run)  # owner rule R44: the Claude review PASS of this head (ADR-0132)
    return {"src": src, "bare": bare, "run": run, "head": head}


def _grant(db, run, n=5):
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="GIT_BRANCH",
                       scope={"repo": "owner/repo", "branch": BRANCH}, ttl_seconds=300, max_requests=n)


def _push(db, env, **kw):
    return git_broker.push_task_branch(db, run_id=env["run"].id, worktree=str(env["src"]), branch=BRANCH,
                                       repo_full_name="owner/repo", remote_url=str(env["bare"]), **kw)


# ----------------------------------------------------------------------------- publication gate
@pytest.mark.parametrize("state", ["RUNNING", "LEASED", "FAILED", "CANCELLED"])
def test_only_review_or_done_candidates_are_published(db, env, state):
    env["run"].execution_status = state
    _grant(db, env["run"])
    with pytest.raises(PolicyDenied) as exc:
        _push(db, env)
    assert exc.value.code == "RUN_NOT_PUBLISHABLE"
    assert _git(env["bare"], "for-each-ref") == ""  # nothing reached the remote


def test_push_pins_the_runtime_recorded_head(db, env):
    _grant(db, env["run"])
    with pytest.raises(PolicyDenied) as exc:
        _push(db, env, expected_head="0" * 40)
    assert exc.value.code == "HEAD_MISMATCH"
    # A commit added to the candidate after the runtime recorded its head is never published.
    (env["src"] / "later.txt").write_text("sneaky\n", encoding="utf-8")
    _git(env["src"], "add", "-A")
    _git(env["src"], "commit", "-q", "-m", "after review")
    res = _push(db, env)
    assert not res["ok"] and "differs" in res["error"]
    assert _git(env["bare"], "for-each-ref") == ""


def test_run_without_runtime_head_is_not_published(db, env):
    env["run"].head_commit = None
    _grant(db, env["run"])
    with pytest.raises(PolicyDenied) as exc:
        _push(db, env)
    assert exc.value.code == "NO_RUNTIME_HEAD"


def test_quarantined_run_needs_release_decision_before_push(db, env):
    run = env["run"]
    run.quarantined = True
    run.quarantine_reasons = [{"path": "backend/aeos/broker/x.py", "rule": "protected-path", "detail": "b"}]
    _grant(db, run)
    with pytest.raises(PolicyDenied) as exc:
        _push(db, env)
    assert exc.value.code == "QUARANTINED"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.git.push_branch",
                                              AuditEvent.result == "DENIED"))
    # A release decision for a different head does not count.
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                    target_hash="f" * 40, domain="PERSONAL", rationale="other patch"))
    db.flush()
    with pytest.raises(PolicyDenied):
        _push(db, env)
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                    target_hash=run.head_commit, domain="PERSONAL", rationale="special review done"))
    db.flush()
    res = _push(db, env)
    assert res["ok"] and _git(env["bare"], "rev-parse", f"refs/heads/{BRANCH}") == env["head"]


def test_push_fetches_from_trusted_git_dir_not_worktree_pointer(db, env, tmp_path):
    """ADR-0054: the broker reads the task branch from the runtime-recorded git dir."""
    decoy = tmp_path / "decoy"
    init_git_repo(decoy)
    run = env["run"]
    run.workspace_ref = str(decoy)  # what the caller passes as worktree must still match the run
    _grant(db, run)
    res = git_broker.push_task_branch(db, run_id=run.id, worktree=str(decoy), branch=BRANCH,
                                      repo_full_name="owner/repo", remote_url=str(env["bare"]))
    assert res["ok"] and res["commit"] == env["head"]  # came from git_dir_ref, not the decoy


# ----------------------------------------------------------------------------- outbox outcome + reconciliation
def test_push_timeout_during_push_is_unknown_not_failed(db, env, monkeypatch):
    real = git_broker._git

    def fake(args, **kw):
        if args and args[0] == "push":
            raise subprocess.TimeoutExpired(cmd="git push", timeout=1)
        return real(args, **kw)

    monkeypatch.setattr(git_broker, "_git", fake)
    _grant(db, env["run"])
    res = _push(db, env)
    se = db.scalar(select(BrokerSideEffect))
    assert res["status"] == "UNKNOWN" and se.status == "UNKNOWN"  # may have landed: reconcile first
    assert se.target["remote_url"] == str(env["bare"]) and se.target["expected_head"] == env["head"]


def test_reconcile_push_reads_the_remote(db, env):
    se = BrokerSideEffect(run_id=env["run"].id, project_id=env["run"].project_id, domain="PERSONAL",
                          kind="PUSH_BRANCH", idempotency_key="push-k", status="PENDING",
                          target={"repo": "owner/repo", "branch": BRANCH, "remote": "local",
                                  "remote_url": str(env["bare"]), "expected_head": env["head"]})
    db.add(se)
    db.flush()
    out = git_broker.reconcile_side_effect(db, se.id, actor="user:o")
    assert out["status"] == "FAILED" and "not present" in out["detail"]  # nothing landed
    se.status = "UNKNOWN"
    _git(env["src"], "push", "-q", str(env["bare"]), f"{BRANCH}:{BRANCH}")
    out = git_broker.reconcile_side_effect(db, se.id, actor="user:o")
    assert out["status"] == "SUCCEEDED" and se.commit_sha == env["head"]
    again = git_broker.reconcile_side_effect(db, se.id, actor="user:o")
    assert again["changed"] is False  # only PENDING/UNKNOWN rows are touched
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.side_effect.reconcile"))


def test_reconcile_pr_reads_github_and_never_writes(db, env, secrets_memory):
    secrets_memory.put("personal/github", TOKEN, actor="test")
    se = BrokerSideEffect(run_id=env["run"].id, project_id=env["run"].project_id, domain="PERSONAL",
                          kind="CREATE_PR", idempotency_key="pr-k", status="UNKNOWN",
                          target={"repo": "owner/repo", "head": BRANCH, "base": "main"})
    db.add(se)
    db.flush()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        lst = mock.get("https://api.github.com/repos/owner/repo/pulls").mock(
            return_value=httpx.Response(200, json=[{"number": 11, "html_url": "https://github.com/owner/repo/pull/11"}]))
        out = git_broker.reconcile_side_effect(db, se.id, actor="user:o")
        assert lst.calls[0].request.url.params["state"] == "all"
        assert all(c.request.method == "GET" for c in mock.calls)  # read-only
    assert out["status"] == "SUCCEEDED" and se.external_id == "11" and TOKEN not in str(out)


def test_reconcile_pr_read_failure_stays_unknown(db, env, secrets_memory):
    secrets_memory.put("personal/github", TOKEN, actor="test")
    se = BrokerSideEffect(run_id=env["run"].id, project_id=env["run"].project_id, domain="PERSONAL",
                          kind="CREATE_PR", idempotency_key="pr-k2", status="UNKNOWN",
                          target={"repo": "owner/repo", "head": BRANCH, "base": "main"})
    db.add(se)
    db.flush()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://api.github.com/repos/owner/repo/pulls").mock(side_effect=httpx.ConnectError("down"))
        out = git_broker.reconcile_side_effect(db, se.id, actor="user:o")
    assert out["status"] == "UNKNOWN" and out["changed"] is False


def test_reconcile_endpoint_scoped_and_admin_only(personal_client, db, env):
    office = BrokerSideEffect(run_id="r-office", project_id="office-p", domain="OFFICE", kind="PUSH_BRANCH",
                              idempotency_key="office-k", status="UNKNOWN", target={"branch": BRANCH})
    db.add(office)
    db.commit()
    assert personal_client.post(f"/api/broker/side-effects/{office.id}/reconcile").status_code == 404


# ----------------------------------------------------------------------------- credential separation (§26)
@pytest.fixture
def proxy(settings, db, secrets_memory, monkeypatch):
    FakeModules().install(monkeypatch)
    secrets_memory.put("workers/openrouter", "sk-or-v1-" + "k" * 48, actor="test")
    secrets_memory.put("office/openrouter", "sk-or-v1-" + "o" * 48, actor="test")
    app = make_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _grant_for(db, *, domain, route_key):
    p = make_project(db, domain=domain)
    run = WorkerRun(project_id=p.id, domain=domain, execution_status="RUNNING", model_route_key=route_key)
    db.add(run)
    db.flush()
    raw, _g = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain=domain, kind="MODEL_PROXY",
                                 scope={"models": {"m": route_key}}, ttl_seconds=600, max_requests=3)
    db.commit()
    return raw


@pytest.mark.parametrize("domain,cred_ref", [("OFFICE", "workers/openrouter"), ("OFFICE", "personal/openrouter"),
                                             ("PERSONAL", "office/openrouter")])
def test_credential_namespace_must_match_domain(proxy, db, domain, cred_ref):
    """An OFFICE run never uses a personal/worker key and a PERSONAL run never an office key,
    whatever the route configuration says (§23 OFFICE default deny, §26 separate paths)."""
    make_route(db, key=f"worker.x-{domain}-{cred_ref.split('/')[0]}", domains=[domain],
               mode_params={"credential_ref": cred_ref})
    token = _grant_for(db, domain=domain, route_key=f"worker.x-{domain}-{cred_ref.split('/')[0]}")
    with respx.mock(assert_all_mocked=True) as mock:
        r = proxy.post("/broker/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
                       headers={"Authorization": f"Bearer {token}"})
        assert len(mock.calls) == 0  # nothing forwarded upstream
    assert r.status_code == 403 and r.json()["error"]["code"] == "CREDENTIAL_DOMAIN"


def test_invalid_credential_ref_fails_closed(proxy, db):
    make_route(db, key="worker.badref", mode_params={"credential_ref": "nonsense/../x"})
    token = _grant_for(db, domain="PERSONAL", route_key="worker.badref")
    r = proxy.post("/broker/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "UPSTREAM_CREDENTIAL_MISSING"


def test_release_for_current_head_found_even_when_newer_stale_decision_exists(db, env):
    """Several decisions for one run: only the one bound to the current head counts, whatever order."""
    run = env["run"]
    run.quarantined = True
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                    target_hash=run.head_commit, domain="PERSONAL", rationale="special review done"))
    db.flush()
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                    target_hash="e" * 40, domain="PERSONAL", rationale="for a later patch"))
    db.flush()
    _grant(db, run)
    assert _push(db, env)["ok"]


def test_reconcile_available_through_the_facade(db, env):
    se = BrokerSideEffect(run_id=env["run"].id, project_id=env["run"].project_id, domain="PERSONAL",
                          kind="PUSH_BRANCH", idempotency_key="push-facade", status="PENDING",
                          target={"repo": "owner/repo", "branch": BRANCH, "remote": "local",
                                  "remote_url": str(env["bare"]), "expected_head": env["head"]})
    db.add(se)
    db.flush()
    assert broker.reconcile_side_effect(db, side_effect_id=se.id, actor="service:delivery")["status"] == "FAILED"
