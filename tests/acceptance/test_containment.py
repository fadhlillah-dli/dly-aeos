"""Worker containment and capability boundaries (§11, §25, §26, §31, ADR-0051). §50 rows:

* 1.12 "File path traversal/symlink keluar sandbox" -> access blocked;
* 1.13 "Worker membaca vault/SSH/native Claude credential" -> not available through the FS/tool/egress
  the worker is given (the process sandbox's OS-level gaps are reported NOT_ENFORCED, never PASS; the
  strong container boundary is BLOCKED on this host);
* 1.34 "PR code mencoba membaca production secret" -> secret not available;
* 3.11 "Worker memanggil binary/tool/endpoint tidak admitted" -> broker/boundary refuses even when the
  component exists on the host.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from aeos.core.paths import repo_root
from aeos.models.audit import AuditEvent
from aeos.models.planning import TaskSpec
from tests.acceptance.fakes import FakeClaude
from tests.acceptance.support import fake_token
from tests.acceptance.world import (
    GIT_TIMEOUT,
    WORKER_ROUTE,
    PlanningResponder,
    admit,
    create_project,
    git_repo,
    plan_content,
    task,
    worker_ready,
)
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

SECRET_ENV = {"ANTHROPIC_API_KEY": "sk-ant-" + "q" * 30, "GITHUB_TOKEN": fake_token(), "VAULT_TOKEN": "hvs." + "v" * 24,
              "BAO_TOKEN": "hvs." + "b" * 24, "OPENROUTER_API_KEY": "sk-or-v1-" + "o" * 40,
              "AWS_SECRET_ACCESS_KEY": "a" * 40, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "c" * 30}


# ---------------------------------------------------------------------------------------------- 1.12
def test_task_write_scope_that_leaves_the_workspace_never_becomes_a_task(personal_client: AuthedClient, db,
                                                                       monkeypatch):
    content = plan_content(tasks=[task("T1", paths=["../../outside/**"]), task("T2", paths=["C:/Users/me/.ssh/**"])])
    FakeClaude(monkeypatch, PlanningResponder(content))
    project = create_project(personal_client, name="Escape")
    from aeos.planning import runner

    plan = personal_client.post(f"/api/projects/{project['id']}/plans",
                                json={"title": "escape", "prompt": "Tulis berkas di luar workspace untuk saya"}).json()
    assert personal_client.post(f"/api/plans/{plan['id']}/run").status_code == 202
    runner.join_all(60)
    detail = personal_client.get(f"/api/plans/{plan['id']}").json()
    assert detail["state"] != "READY_FOR_HUMAN_REVIEW", "an unsafe write scope never reaches the human as approvable"
    assert "DAG" in (detail["state_reason"] or "") or detail["state"] in ("PLANNING_FAILED", "REVIEW_FAILED")
    assert db.scalar(select(TaskSpec).where(TaskSpec.project_id == project["id"])) is None


def test_candidate_symlink_and_traversal_are_never_materialised_or_accepted(tmp_path: Path, settings):
    from aeos.verification.procs import GitReader
    from aeos.worker_runtime.scope import ChangedPath, ScopePathError, check_scope, normalize

    repo = tmp_path / "candidate"
    git_repo(repo, {"src/app.py": "print('ok')\n"})
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"../../../outside/secret",
                          capture_output=True, check=True, timeout=GIT_TIMEOUT).stdout.decode().strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"120000,{blob},src/escape"], cwd=repo, check=True,
                   capture_output=True, timeout=GIT_TIMEOUT)
    subprocess.run(["git", "-c", "user.email=a@example.invalid", "-c", "user.name=a", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=", "commit", "-q", "-m", "symlink"], cwd=repo, check=True,
                   capture_output=True, timeout=GIT_TIMEOUT)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                          check=True, timeout=GIT_TIMEOUT).stdout.strip()
    dest = tmp_path / "snapshot"
    dest.mkdir()
    info = GitReader(repo).export_tree(head, dest, max_bytes=10 * 1024 * 1024)
    assert {"path": "src/escape", "reason": "symlink not materialised"} in info["skipped"]
    assert not (dest / "src" / "escape").exists() and (dest / "src" / "app.py").is_file()
    for bad in ("../escape.txt", "src/../../x", "C:/Windows/x", "/etc/passwd"):
        with pytest.raises(ScopePathError):
            normalize(bad)
    res = check_scope([ChangedPath(path="../escape.txt", status="A"), ChangedPath(path="src/escape", status="A",
                                                                                  mode="120000")],
                      allowed=["src/**"], forbidden=[], protected=())
    assert any(v["rule"] == "unsafe-path" for v in res.violations)
    assert any(h["rule"] == "symlink" for h in res.protected_hits) and res.quarantine


def test_upload_archive_with_traversal_or_symlink_is_rejected(personal_client: AuthedClient, settings):
    import io
    import stat
    import zipfile

    project = create_project(personal_client, name="Zip")

    def zipped(entries) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in entries:
                z.writestr(name, data)
        return buf.getvalue()

    for evil in ("../evil.txt", "a/../../evil.txt", "C:/Windows/evil.txt"):
        r = personal_client.post(f"/api/projects/{project['id']}/attachments",
                                 files={"file": ("b.zip", zipped([("ok.md", b"# ok"), (evil, b"x")]), "application/zip")})
        assert r.status_code == 422 and r.json()["error"]["code"] == "ARCHIVE_UNSAFE", evil
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    r = personal_client.post(f"/api/projects/{project['id']}/attachments",
                             files={"file": ("l.zip", zipped([(link, b"/etc/passwd")]), "application/zip")})
    assert r.status_code == 422
    assert not (settings.data_dir / "evil.txt").exists()


# ---------------------------------------------------------------------------------------------- 1.13
def test_worker_environment_and_tools_never_carry_vault_ssh_or_claude_credentials(tmp_path: Path, monkeypatch):
    from aeos.worker_runtime import opencode, processes

    for k, v in SECRET_ENV.items():
        monkeypatch.setenv(k, v)
    dirs = processes.SandboxDirs.create(tmp_path / "sbx")
    env = processes.build_sandbox_env(dirs, run_id="run-1")
    leaked = [k for k in SECRET_ENV if k in env] + [k for k, v in env.items() if v in SECRET_ENV.values()]
    assert leaked == []
    assert env["HOME"] == str(dirs.home) and env["USERPROFILE"] == str(dirs.home)
    assert Path(env["APPDATA"]).is_relative_to(dirs.root)  # ~/.claude, ~/.ssh, keyrings resolve inside the sandbox
    assert env["GIT_CONFIG_GLOBAL"].startswith(str(dirs.home)) and env["GIT_TERMINAL_PROMPT"] == "0"
    cfg = opencode.build_config(broker_base_url="http://127.0.0.1:8787/broker/v1", token="aeoscap_" + "t" * 32,
                                model="worker-acc-free", tools_allowed=[], egress_allowed=[])
    perm = cfg["permission"]
    assert perm["*"] == "deny" and perm["external_directory"] == "deny"
    assert perm["bash"] == "deny" and perm["webfetch"] == "deny" and perm["task"] == "deny"
    provider = cfg["provider"]["aeos-broker"]["options"]
    assert provider["apiKey"].startswith("aeoscap_"), "only a run-bound capability token, never an upstream key"
    assert provider["baseURL"].startswith("http://127.0.0.1:")
    assert cfg["mcp"] == {} and cfg["plugin"] == []


def test_process_sandbox_containment_is_reported_honestly(personal_client: AuthedClient, settings):
    r = personal_client.post("/api/worker-runtime/containment", json={"kind": "process"})
    assert r.status_code == 200, r.text
    body = r.json()
    report = body.get("report", body)
    by = {x["check"]: x["status"] for x in report["results"]}
    for passing in ("env_secrets", "home_redirected", "home_ssh_redirected", "cancel_kills_tree"):
        assert by[passing] == "PASS", (passing, by)
    for weak in ("read_data_db", "real_ssh_dir", "keyring_unreachable", "egress_non_broker", "write_outside_worktree"):
        assert by[weak] in ("NOT_ENFORCED", "PASS"), (weak, by)
    if "NOT_ENFORCED" in by.values():
        assert report["overall"] == "WEAK", "a NOT_ENFORCED boundary is never summarised as PASS"
    assert report["overall"] != "FAIL" and report["evidence_level"] == "E2"
    sandbox = personal_client.get("/api/worker-runtime/sandbox").json()
    levels = sandbox.get("capabilities", sandbox).get("levels", sandbox.get("capabilities", {}))
    assert levels["process"]["strength"] == "WEAK"


@pytest.mark.integration
def test_container_sandbox_blocks_filesystem_and_egress(settings):
    """§25 minimum controls proven on a real Docker host: the containment suite runs INSIDE the pinned worker
    image (non-root, read-only root, only worktree/config mounted, internal broker-only network, no docker
    socket / keyring / credential helpers, cgroup limits, kill by label). Skips cleanly without Docker or the
    image; a host that has both must PASS (never a fake)."""
    from aeos.worker_runtime import container, sandbox

    sandbox._cache.update(at=0.0, value=None)
    caps = sandbox.sandbox_capabilities(refresh=True)["levels"]["container"]
    if not caps["available"]:
        pytest.skip(f"BLOCKED-docker-containment: {caps['evidence']} (install/start Docker to prove the container "
                    "boundary on this host, ADR-0051)")
    image = container.image_status()
    if not image["present"]:
        pytest.skip(f"BLOCKED-docker-containment: {image['evidence']}")
    report = container.containment_report(domain="PERSONAL")
    failing = {r["check"]: r["evidence"] for r in report["results"] if r["status"] != "PASS"}
    assert report["overall"] == "PASS", failing
    for check in ("read_data_db", "real_ssh_dir", "egress_non_broker", "keyring_unreachable", "write_outside_worktree",
                  "docker_socket_absent", "non_root", "cancel_kills_tree"):
        assert next(r for r in report["results"] if r["check"] == check)["status"] == "PASS", check


# formerly strict xfail BUG_CONTAINMENT_REPORT (containment.report misread the runtime report); fixed by W3
def test_doctor_containment_check_reflects_the_runtime_report(personal_client: AuthedClient, db, host, vault):
    from aeos.worker_runtime import facade as runtime

    report = runtime.containment_report("process")
    real_failures = sorted(r["check"] for r in report["results"] if r["status"] == "FAIL")
    r = personal_client.post("/api/doctor/run", json={"group": "CONTAINMENT", "functional": True})
    assert r.status_code == 200, r.text
    check = next(i for i in r.json()["items"] if i["check_id"] == "containment.report")
    assert sorted(check["observed_evidence"]["failed"]) == real_failures


# ---------------------------------------------------------------------------------------------- 1.34
def test_pr_code_under_the_verifier_sees_no_production_secret(tmp_path: Path, monkeypatch):
    from aeos.verification.procs import scrubbed_env

    for k, v in SECRET_ENV.items():
        monkeypatch.setenv(k, v)
    env = scrubbed_env(pythonpath=[tmp_path / "backend"], home=tmp_path / "h", tmp=tmp_path / "t",
                       data_dir=tmp_path / "d", disable_plugin_autoload=True)
    assert not [k for k in SECRET_ENV if k in env]
    assert not [k for k, v in env.items() if v in SECRET_ENV.values()]
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"
    assert env["AEOS_DATA_DIR"] == str(tmp_path / "d")  # never the controller's data dir / vault bootstrap
    probe = ("import os, json; print(json.dumps(sorted(k for k in os.environ if any(s in k for s in "
             "('TOKEN','KEY','SECRET','ANTHROPIC','VAULT','BAO')))))")
    import sys

    out = subprocess.run([sys.executable, "-I", "-c", probe], env=env, capture_output=True, text=True, check=True,
                         cwd=tmp_path, timeout=120)
    assert out.stdout.strip() in ("[]", '["PYTHON_KEYRING_BACKEND"]')


def test_pr_ci_workflows_get_no_secrets_and_a_read_only_token():
    workflows = sorted((Path(repo_root()) / ".github" / "workflows").glob("*.y*ml"))
    assert workflows, "the platform CI workflow is missing"
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        on = data.get("on", data.get(True))
        triggers = set(on) if isinstance(on, (dict, list)) else {on}
        assert "pull_request_target" not in triggers and "workflow_run" not in triggers, path.name
        assert not re.search(r"\$\{\{\s*secrets\.", text), path.name
        perms = data.get("permissions")
        assert perms is not None and "write" not in str(perms), (path.name, perms)


@pytest.mark.integration
def test_github_hosted_pr_run_has_no_secret_access():
    pytest.skip("BLOCKED-real-github: secret isolation of a real GitHub-hosted PR run needs the owner's repository "
                "and a fork PR; only the static workflow guard and the verifier env are proven here")


# ---------------------------------------------------------------------------------------------- 3.11
def test_worker_calls_outside_its_capability_are_refused(personal_client: AuthedClient, db, monkeypatch, vault):
    from aeos.broker import facade as broker
    from aeos.broker.git_broker import validate_branch, validate_refspec
    from aeos.core.errors import AeosError
    from aeos.models.routing import ModelRoute

    w = worker_ready(personal_client, db, monkeypatch, tasks=[task("T1")])
    run_id = admit(db)[0]
    token, _ = broker.issue_grant(db, run_id=run_id, project_id=w["project"]["id"], domain="PERSONAL",
                                  kind="MODEL_PROXY", scope={"models": {"m": WORKER_ROUTE}}, ttl_seconds=600)
    db.commit()
    c = personal_client.client
    cookie = c.post("/broker/v1/chat/completions", headers={"Authorization": f"Bearer {token}",
                                                            "X-CSRF-Token": personal_client.csrf or ""},
                    json={"model": "m", "messages": []})
    assert cookie.status_code == 401 and cookie.json()["error"]["code"] == "COOKIE_AUTH_REJECTED"
    saved = dict(c.cookies)
    c.cookies.clear()  # the worker side: no dashboard session, only its capability token
    missing = c.post("/broker/v1/chat/completions", json={"model": "m", "messages": []})
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "CAPABILITY_TOKEN_MISSING"
    other = c.post("/broker/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert other.status_code == 404 and other.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    # the route exists on the host, but is not admitted any more -> refused at the boundary
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == WORKER_ROUTE))
    c.cookies.update(saved)
    assert personal_client.post(f"/api/routes/{WORKER_ROUTE}/disable", json={"reason": "not admitted"}).status_code == 200
    db.expire_all()
    c.cookies.clear()
    disabled = c.post("/broker/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                      json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert disabled.status_code == 403 and disabled.json()["error"]["code"] in ("ROUTE_NOT_APPROVED", "ROUTE_DOMAIN")
    assert route is not None
    denials = db.scalars(select(AuditEvent).where(AuditEvent.action == "broker.model_proxy.deny")).all()
    assert len(denials) >= 2 and all(a.result == "DENIED" for a in denials)
    # git: only aeos/* task branches, never the default branch, never force/delete
    for bad in ("main", "refs/heads/main", "feature/x"):
        with pytest.raises((AeosError, ValueError)):
            validate_branch(bad)
    for bad in ("+aeos/t1:aeos/t1", ":aeos/t1", "aeos/t1:main"):
        with pytest.raises((AeosError, ValueError)):
            validate_refspec(bad)
    # tools: a binary present on the host (bash/curl) is still denied unless the TaskSpec grants it
    from aeos.worker_runtime import opencode

    cfg = opencode.build_config(broker_base_url="http://127.0.0.1:8787/broker/v1", token="aeoscap_x" * 4,
                                model="m", tools_allowed=["read"], egress_allowed=[])
    assert cfg["permission"]["bash"] == "deny" and cfg["permission"]["webfetch"] == "deny"
    assert os.environ.get("AEOS_RUN_ID") is None
