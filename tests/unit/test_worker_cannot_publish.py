"""A worker cannot commit to the real repository, push or open a pull request itself (owner rule R44, 2026-10-29,
ADR-0132; master §25/§26: workers hold no remote credential, only the trusted broker writes ``aeos/*`` branches and
opens PRs).

What enforces it, seam by seam (each test has a near miss that shows the same operation works where it is allowed):

* git in the worker's environment obtains no code-host credential, even when the owner's own home holds one: the
  sandbox environment redirects HOME, empties the global git config, ignores the system config (where a credential
  helper such as the OS keychain lives) and disables prompts;
* the isolated candidate (self-build) has no remote at all, so ``git push`` has no destination;
* the broker's only worker-facing HTTP surface is the model proxy (``/broker/v1/models`` and ``/chat/completions``):
  a capability token of the push/PR kinds is refused there, and delivery's publication route needs the owner's
  session, never a capability token.

Elsewhere: the worker gets no shell in the process sandbox and ``bash`` is denied in its OpenCode config by default
(tests/unit/test_worker_runtime_sandbox.py, tests/acceptance/test_containment.py); the container sandbox has only the
broker gateway on an internal network (tests/acceptance/test_containment.py, integration); the broker publishes only
a head with a Claude review PASS (tests/unit/test_broker_publication_review.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import grants
from aeos.core.enums import Domain
from aeos.core.errors import Forbidden
from aeos.core.paths import DataLayout
from aeos.models.broker_ext import BrokerSideEffect
from aeos.worker_runtime import processes, workspace
from tests.unit.test_workforce_support import init_git_repo

CREDENTIAL_QUERY = "protocol=https\nhost=github.com\n\n"
# A placeholder, not a credential shape: the test only needs to see whether git can obtain *something*.
PLACEHOLDER = "placeholder-not-a-credential"


def _git(args: list[str], *, cwd: Path, env: dict[str, str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, input=stdin, capture_output=True, text=True,
                          timeout=60, check=False)


def _owner_home(tmp_path: Path) -> Path:
    """The owner's home as it might look: a global git config whose credential helper holds a GitHub login."""
    home = tmp_path / "owner-home"
    (home / ".config").mkdir(parents=True)
    store = home / ".git-credentials"
    store.write_text(f"https://owner:{PLACEHOLDER}@github.com\n", encoding="utf-8")
    (home / ".gitconfig").write_text(f"[credential]\n\thelper = store --file={store.as_posix()}\n", encoding="utf-8")
    return home


def _owner_env(home: Path) -> dict[str, str]:
    """A shell of the owner: real PATH, this home. The system git config stays off in tests too (it may name the OS
    keychain, which no test may reach - LESSONS L20)."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC")}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def test_git_in_the_worker_environment_gets_no_credential_even_when_the_owner_has_one(tmp_path, monkeypatch):
    home = _owner_home(tmp_path)
    near_miss = _git(["credential", "fill"], cwd=tmp_path, env=_owner_env(home), stdin=CREDENTIAL_QUERY)
    assert near_miss.returncode == 0 and f"password={PLACEHOLDER}" in near_miss.stdout, \
        "the owner's own shell does reach the stored login (so the refusal below is for the right reason)"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    env = processes.build_sandbox_env(processes.SandboxDirs.create(tmp_path / "sbx"), run_id="run-cannot-push")
    assert env["HOME"] != str(home) and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    worker = _git(["credential", "fill"], cwd=tmp_path, env=env, stdin=CREDENTIAL_QUERY)
    assert worker.returncode != 0, "git in the worker environment must not obtain a code-host login"
    assert PLACEHOLDER not in worker.stdout + worker.stderr and "password=" not in worker.stdout
    helpers = _git(["config", "--get-all", "credential.helper"], cwd=tmp_path, env=env)
    assert helpers.stdout.strip() == "", "no credential helper is visible to the worker"


def test_the_isolated_candidate_has_no_remote_so_a_worker_push_has_no_destination(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True, capture_output=True, timeout=60)
    src = tmp_path / "source"
    init_git_repo(src)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=src, check=True, capture_output=True,
                   timeout=60)
    owner = _owner_env(tmp_path / "owner-home-empty")
    near_miss = _git(["push", "--dry-run", "origin", "HEAD:refs/heads/aeos/probe"], cwd=src, env=owner)
    assert near_miss.returncode == 0, "the source repository itself has a push destination"
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="proj",
                                    run_id="run-0002-bbbb", task_key="Task B", source_repo=src, base_commit=None,
                                    isolated=True)
    env = processes.build_sandbox_env(processes.SandboxDirs.create(tmp_path / "sbx"), run_id="run-0002-bbbb")
    assert _git(["remote"], cwd=ws.repo, env=env).stdout.strip() == ""
    pushed = _git(["push"], cwd=ws.repo, env=env)
    assert pushed.returncode != 0 and "No configured push destination" in pushed.stderr, pushed.stderr
    pushed = _git(["push", "origin", "HEAD:refs/heads/aeos/probe"], cwd=ws.repo, env=env)
    assert pushed.returncode != 0 and "origin" in pushed.stderr
    assert _git(["for-each-ref"], cwd=origin, env=owner).stdout.strip() == "", "nothing reached the source's remote"


def _routes_under(app, prefix: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path, ops in (app.openapi().get("paths") or {}).items():
        if path.startswith(prefix):
            found |= {(m.upper(), path) for m in ops}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(prefix):
            found |= {(m, route.path) for m in route.methods}
    return found


@pytest.mark.parametrize("kind,scope", [("GIT_BRANCH", {"repo": "owner/repo", "branch": "aeos/t-1"}),
                                        ("PR_CREATE", {"repo": "owner/repo", "head": "aeos/t-1", "base": "main"})])
def test_a_capability_token_reaches_no_publication_route(db, app, client, kind, scope):
    raw, _g = broker.issue_grant(db, run_id="run-pub", project_id="proj-pub", domain="PERSONAL", kind=kind,
                                 scope=scope, ttl_seconds=300)
    model_raw, _m = broker.issue_grant(db, run_id="run-pub", project_id="proj-pub", domain="PERSONAL",
                                       kind="MODEL_PROXY", scope={"models": {"m": "worker.x"}}, ttl_seconds=300)
    db.commit()
    # the worker-facing broker surface is the model proxy, nothing that writes a branch or a pull request
    assert _routes_under(app, "/broker/") == {("GET", "/broker/v1/models"), ("POST", "/broker/v1/chat/completions")}
    with pytest.raises(Forbidden):  # a push/PR capability is not a model-proxy capability
        grants.authenticate(db, raw, kind="MODEL_PROXY")
    assert grants.authenticate(db, model_raw, kind="MODEL_PROXY").kind == "MODEL_PROXY"  # near miss
    # delivery's publication route needs the owner's session; a capability bearer token is no session
    r = client.client.post("/api/tasks/any-task/prepare-pr", json={}, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED", r.text
    assert db.scalar(select(BrokerSideEffect)) is None
