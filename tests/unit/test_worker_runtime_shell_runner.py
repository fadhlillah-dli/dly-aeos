"""W6-117 §42 "Penghematan: output command ringkas ... simpan log asli": a shell granted to a container worker runs
only through the AEOS runner, which returns a bounded head/tail to the engine context and keeps the complete
output as a per-command log the runtime collects as evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from aeos.worker_runtime import container, opencode

LINES = 500


def test_bash_grants_become_runner_only_permission_rules():
    runner = container.SHELL_RUNNER_COMMAND
    assert opencode._bash_rules(["bash"], runner=runner) == {"*": "deny", f"{runner} *": "allow"}
    assert opencode._bash_rules(["bash:npm test*", "bash:pytest -q*"], runner=runner) == {
        "*": "deny", f"{runner} npm test*": "allow", f"{runner} pytest -q*": "allow"}
    assert opencode._bash_rules(["read"], runner=runner) == "deny"
    # without a runner (never admitted: a shell needs the container sandbox) the old shape is unchanged
    assert opencode._bash_rules(["bash"]) == "allow" and opencode._bash_rules([]) == "deny"
    cfg = opencode.build_config(broker_base_url="http://127.0.0.1:9/broker/v1", token="aeoscap_" + "t" * 32,
                                model="m", tools_allowed=["bash"], egress_allowed=[], shell_runner=runner)
    assert cfg["permission"]["bash"] == {"*": "deny", f"{runner} *": "allow"}


def _check_output(stdout: str, log_dir: Path, code: int, expect_code: int) -> None:
    out = stdout.splitlines()
    assert code == expect_code
    assert out[:container.RUNNER_HEAD_LINES] == [f"line {i}" for i in range(container.RUNNER_HEAD_LINES)]
    tail = [f"line {i}" for i in range(LINES - container.RUNNER_TAIL_LINES, LINES)]
    assert out[-container.RUNNER_TAIL_LINES - 1:-1] == tail
    omitted = LINES - container.RUNNER_HEAD_LINES - container.RUNNER_TAIL_LINES
    assert f"[aeos-run: {omitted} line(s) omitted" in stdout and f"exit {expect_code}, {LINES} line(s)" in out[-1]
    assert len(out) == container.RUNNER_HEAD_LINES + container.RUNNER_TAIL_LINES + 2  # bounded
    logs = list(log_dir.glob("*.log"))
    assert len(logs) == 1
    full = logs[0].read_text(encoding="utf-8")
    assert all(f"line {i}\n" in full for i in range(LINES)) and f"[aeos-run: exit {expect_code}]" in full


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_runner_bounds_the_output_and_keeps_the_full_log(tmp_path):
    """The runner itself (any OS with node): a 500-line command returns 40 head + 80 tail lines and its exit code;
    the log keeps every line."""
    script = tmp_path / container.SHELL_RUNNER_FILE
    script.write_text(container.SHELL_RUNNER_JS, encoding="utf-8")
    log_dir = tmp_path / "cmdlog"
    node = shutil.which("node")
    assert node
    env = {**os.environ, "AEOSRUN_LOG_DIR": str(log_dir), "AEOSRUN_SHELL": json.dumps([node, "-e"])}
    code = f"for (let i = 0; i < {LINES}; i++) console.log('line ' + i); process.exitCode = 3;"
    proc = subprocess.run([node, str(script), code], capture_output=True, text=True, env=env, timeout=60, check=False)
    _check_output(proc.stdout, log_dir, proc.returncode, 3)
    long = subprocess.run([node, str(script), "console.log('x'.repeat(5000))"], capture_output=True, text=True,
                          env=env, timeout=60, check=False)
    assert long.returncode == 0 and "[line truncated]" in long.stdout and len(long.stdout) < 1000


@pytest.mark.integration
def test_runner_inside_the_real_worker_image(tmp_path):
    """The same runner inside the pinned worker image on this host (Docker + dly-aeos-worker present): hardened
    container (non-root, read-only root, config mounted read-only, no network), /bin/sh, log in the worktree."""
    caps = container.image_status() if shutil.which("docker") else {"present": False, "evidence": "no docker CLI"}
    if not caps.get("present"):
        pytest.skip(f"BLOCKED-docker: {caps.get('evidence')}")
    from aeos.worker_runtime.sandbox import container_command

    wt, cfg = tmp_path / "wt", tmp_path / "cfg"
    wt.mkdir()
    cfg.mkdir()
    container.write_shell_runner(cfg)
    rid = f"w7f-runner-{uuid.uuid4().hex[:8]}"
    argv = container_command(image=container.image_tag(), run_id=rid, worktree=str(wt), config_dir=str(cfg),
                             inner_argv=[f"{container.CONTAINER_CONFIG}/{container.SHELL_RUNNER_FILE}",
                                         f"i=0; while [ $i -lt {LINES} ]; do echo line $i; i=$((i+1)); done; exit 4"],
                             env=container.worker_env(rid, {}), network="none", entrypoint="node",
                             docker=container.docker_cmd())
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=container.docker_cli_env(),
                              stdin=subprocess.DEVNULL, check=False)
    finally:
        container.teardown(rid)
    _check_output(proc.stdout, wt / container.COMMAND_LOG_DIR, proc.returncode, 4)
