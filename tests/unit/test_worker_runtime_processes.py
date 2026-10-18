"""Sandbox env scrubbing + process-tree kill on this OS (§25, §26, §50 'Worker membaca vault/SSH/native
Claude credential', 'Cancel saat ada child process')."""

from __future__ import annotations

import os
import sys

import psutil

from aeos.models.workforce import WorkerRun
from aeos.worker_runtime import processes
from tests.unit.test_workforce_support import SLEEP_TREE


def test_env_has_no_secrets_and_home_is_redirected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 40)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "b" * 36)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "c" * 40)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x" * 30)
    monkeypatch.setenv("AEOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("VAULT_TOKEN", "hvs." + "d" * 30)
    monkeypatch.setenv("LANG", "sk-" + "e" * 30)  # allow-listed name, secret-looking value
    dirs = processes.SandboxDirs.create(tmp_path / "run")
    env = processes.build_sandbox_env(dirs, run_id="run-xyz")
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    for bad in ("OPENROUTER", "GITHUB_TOKEN", "ANTHROPIC", "AWS_SECRET", "AEOS_DATA_DIR", "SSH_AUTH_SOCK", "VAULT"):
        assert bad not in joined
    assert "sk-" not in joined and "ghp_" not in joined
    assert "LANG" not in env
    for k in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
              "TEMP", "TMP", "TMPDIR"):
        assert env[k].startswith(str(dirs.root)), k
    assert env["AEOS_RUN_ID"] == "run-xyz" and env["GIT_TERMINAL_PROMPT"] == "0"
    if sys.platform == "win32":
        assert (env["HOMEDRIVE"] + env["HOMEPATH"]).lower() == str(dirs.home).lower()


def test_extra_secret_rejected_even_if_caller_passes_it(tmp_path):
    dirs = processes.SandboxDirs.create(tmp_path / "run")
    env = processes.build_sandbox_env(dirs, run_id="r", extra={"MY_TOKEN": "abc", "OPENCODE_CONFIG": str(tmp_path),
                                                               "SAFE": "sk-or-v1-" + "z" * 40})
    assert "MY_TOKEN" not in env and "SAFE" not in env and env["OPENCODE_CONFIG"] == str(tmp_path)


def _grandchild_alive(gpid: int) -> bool:
    try:
        return psutil.Process(gpid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def test_kill_run_processes_kills_child_and_grandchild(tmp_path):
    dirs = processes.SandboxDirs.create(tmp_path / "run")
    env = processes.build_sandbox_env(dirs, run_id="kill-1")
    mp = processes.spawn("kill-1", [sys.executable, "-c", SLEEP_TREE], cwd=tmp_path, env=env)
    gpid = int(mp.stdout.readline().decode().strip())
    assert processes.is_alive(mp.pid, mp.create_time) and _grandchild_alive(gpid)
    res = processes.kill_run_processes("kill-1")
    assert res["found"] and res["remaining"] == []
    assert not processes.is_alive(mp.pid, mp.create_time)
    assert not _grandchild_alive(gpid)
    assert processes.registered("kill-1") == []
    again = processes.kill_run_processes("kill-1")  # idempotent
    assert again["remaining"] == []


def test_kill_after_registry_loss_uses_persisted_pid(db, tmp_path):
    """Controller crash: the in-memory registry is gone; pid + create_time from WorkerRun are used."""
    dirs = processes.SandboxDirs.create(tmp_path / "run")
    run = WorkerRun(project_id="p", domain="PERSONAL", execution_status="RUNNING")
    db.add(run)
    db.flush()
    mp = processes.spawn(run.id, [sys.executable, "-c", SLEEP_TREE], cwd=tmp_path,
                         env=processes.build_sandbox_env(dirs, run_id=run.id))
    gpid = int(mp.stdout.readline().decode().strip())
    run.pid, run.pid_create_time = mp.pid, mp.create_time
    db.commit()
    with processes._registry_lock:  # simulate the crash: registry entry lost, job handle not closed
        processes._registry.pop(run.id)
    res = processes.kill_run_processes(run.id)
    assert res["remaining"] == []
    assert not processes.is_alive(mp.pid, mp.create_time) and not _grandchild_alive(gpid)


def test_spawn_rejects_shell_strings(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        processes.spawn("x", "echo hi", cwd=tmp_path, env=dict(os.environ))  # type: ignore[arg-type]
