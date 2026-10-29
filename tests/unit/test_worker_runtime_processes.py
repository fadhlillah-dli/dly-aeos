"""Sandbox env scrubbing + process-tree kill on this OS (§25, §26, §50 'Worker membaca vault/SSH/native
Claude credential', 'Cancel saat ada child process')."""

from __future__ import annotations

import os
import sys

import psutil
import pytest

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


def test_tree_usage_observes_the_whole_tree(tmp_path):
    """Resource watchdog input (§21/§25): RSS + process count of the run's tree."""
    dirs = processes.SandboxDirs.create(tmp_path / "sbx")
    env = processes.build_sandbox_env(dirs, run_id="usage-run")
    code = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n" \
           "print('ok', flush=True)\ntime.sleep(30)\n"
    mp = processes.spawn("usage-run", [sys.executable, "-c", code], cwd=tmp_path, env=env)
    try:
        assert mp.stdout.readline().strip() == b"ok"
        u = processes.tree_usage("usage-run")
        assert u["processes"] >= 2 and u["rss_bytes"] > 0
    finally:
        processes.kill_run_processes("usage-run")
    assert processes.tree_usage("usage-run") == {"rss_bytes": 0, "processes": 0}


def test_resource_limits_from_taskspec_are_clamped():
    lim = processes.ResourceLimits.from_task({"max_memory_mb": 10, "max_processes": 10_000})
    assert lim.max_memory_mb == 64 and lim.max_processes == 4096
    assert processes.ResourceLimits.from_task(None) == processes.ResourceLimits()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object kernel backstop")
def test_windows_job_object_caps_the_process_count(tmp_path):
    script = ("import subprocess, sys\nok = 0\nkeep = []\nfor _ in range(5):\n    try:\n"
              "        keep.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))\n"
              "        ok += 1\n    except OSError:\n        pass\nprint(ok, flush=True)\n")
    dirs = processes.SandboxDirs.create(tmp_path / "sbx")
    env = processes.build_sandbox_env(dirs, run_id="job-limit")
    mp = processes.spawn("job-limit", [sys.executable, "-c", script], cwd=tmp_path, env=env,
                         limits=processes.ResourceLimits(max_memory_mb=512, max_processes=3))
    try:
        line = mp.stdout.readline().decode().strip()
    finally:
        processes.kill_run_processes("job-limit")
    assert line.isdigit() and int(line) < 5  # the job refused process creation beyond its cap


def test_cpu_rate_is_the_share_of_all_processors():
    """W6-055: JOBOBJECT_CPU_RATE_CONTROL_INFORMATION.CpuRate is 1/100 percent of ALL processors' cycles."""
    assert processes.cpu_rate_for(1, 8) == 1250 and processes.cpu_rate_for(2, 4) == 5000
    assert processes.cpu_rate_for(16, 8) == 10000 and processes.cpu_rate_for(0.0001, 64) == 1
    assert processes.cpu_rate_for(1, None) == 10000
    assert processes.ResourceLimits.from_task({"max_cpus": 1}).max_cpus == 1.0
    assert processes.ResourceLimits.from_task({}).max_cpus is None
    assert processes.ResourceLimits.from_task({"max_cpus": "x"}).max_cpus is None
    assert processes.ResourceLimits.from_task({"max_cpus": -2}).max_cpus is None


def test_taskspec_resource_caps_are_one_value_for_both_sandboxes():
    """W6-055: max_processes is carried in every TaskSpec, so the process sandbox and the container enforce the same
    cap (they used to fall back to different runtime defaults, 64 vs 256)."""
    from aeos.planning import dag
    from aeos.worker_runtime import container, facade

    lim = dict(dag.RESOURCE_LIMITS)
    assert "max_processes" in lim
    assert processes.ResourceLimits.from_task(lim).max_processes == container.limits_for(lim)["pids"] \
        == lim["max_processes"]
    proc = facade.enforced_run_limits(lim, sandbox_kind="process")
    assert proc["cpus"] == float(lim["max_cpus"])
    assert proc["cpu_enforced"] is processes.cpu_cap_supported() is (sys.platform == "win32")
    assert ("CPU-rate hard cap" in proc["enforced_by"]) is (sys.platform == "win32")
    # without a max_cpus there is nothing to cap: never reported as enforced
    assert facade.enforced_run_limits({}, sandbox_kind="process")["cpu_enforced"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object CPU rate control")
def test_windows_job_object_hard_caps_cpu(tmp_path):
    """The kernel holds the CPU-rate hard cap for the worker's job (queried back from the job object)."""
    dirs = processes.SandboxDirs.create(tmp_path / "sbx")
    env = processes.build_sandbox_env(dirs, run_id="cpu-cap")
    mp = processes.spawn("cpu-cap", [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env=env,
                         limits=processes.ResourceLimits(max_memory_mb=512, max_processes=8, max_cpus=1))
    try:
        rate = processes.cpu_rate_for(1, os.cpu_count())
        assert mp.cpu_rate == rate and mp.job is not None
        assert processes._win.query_cpu_rate(mp.job) == (
            processes._win.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | processes._win.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, rate)
    finally:
        processes.kill_run_processes("cpu-cap")
    # without max_cpus no CPU cap is claimed
    mp2 = processes.spawn("cpu-none", [sys.executable, "-c", "pass"], cwd=tmp_path, env=env,
                          limits=processes.ResourceLimits(max_memory_mb=512, max_processes=8))
    try:
        assert mp2.cpu_rate is None
    finally:
        processes.kill_run_processes("cpu-none")


def test_spawn_rejects_shell_strings(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        processes.spawn("x", "echo hi", cwd=tmp_path, env=dict(os.environ))  # type: ignore[arg-type]


class _SweepProcess:
    """A stand-in for ``psutil.Process`` in the marker sweep: its environment, or the error reading it raises."""

    def __init__(self, pid: int, environ: dict[str, str] | None = None, error: BaseException | None = None):
        self.pid, self._environ, self._error = pid, environ or {}, error

    def environ(self) -> dict[str, str]:
        if self._error is not None:
            raise self._error
        return self._environ


def test_the_marker_sweep_survives_a_process_the_kernel_is_tearing_down(monkeypatch):
    """LESSONS L15: on macOS psutil raises SystemError reading the environment of a process that is exiting
    (sysctl(KERN_PROCARGS2) fails with errno 0). One such unrelated process must not abort the kill sweep - the run's
    own marked process is still found - and an unreadable process is never taken for the run's."""
    run_id = "run-sweep-0001"
    dying = _SweepProcess(101, error=SystemError("<built-in function proc_environ> returned a result with an "
                                                 "exception set"))
    hidden = _SweepProcess(102, error=psutil.AccessDenied(102))
    marked = _SweepProcess(103, {processes.RUN_MARKER: run_id})
    other = _SweepProcess(104, {processes.RUN_MARKER: "run-other"})
    monkeypatch.setattr(processes.psutil, "process_iter", lambda attrs=None: iter([dying, hidden, marked, other]))
    assert processes._marker_sweep(run_id) == [marked]
