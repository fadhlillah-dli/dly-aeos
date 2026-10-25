"""Bootstrap scripts (master §06/§07): a failed required step stops the bootstrap with a non-zero exit.

Windows PowerShell 5.1 does not stop on a native non-zero exit even with ``$ErrorActionPreference =
'Stop'``, so every required native call in bootstrap.ps1 must check ``$LASTEXITCODE``; bootstrap.sh
relies on ``set -euo pipefail``. The CI ``bootstrap`` job runs both scripts for real (and a negative
case) - see .github/workflows/ci.yml."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

REQUIRED_STOP = ("db upgrade", "seed")
# The macOS/Linux entry points README.md and docs/SETUP_MACOS.md tell users to run as ./<path>.
POSIX_ENTRY_POINTS = {"aeos", "scripts/bootstrap.sh", "scripts/dev.sh"}


def _lines(name: str) -> list[str]:
    return (repo_root() / "scripts" / name).read_text(encoding="utf-8").splitlines()


def test_powershell_bootstrap_checks_every_native_exit_code():
    lines = _lines("bootstrap.ps1")
    native = [i for i, ln in enumerate(lines) if re.search(r"^\s*&\s+(\.\\\.venv\\Scripts\\(aeos|python)\.exe|npm\.cmd)", ln)]
    assert native, "no native calls found"
    unchecked = []
    for i in native:
        cmd = lines[i].strip()
        if "--upgrade pip" in cmd or "component_status.py" in cmd:
            continue  # best effort / status only
        follow = next((ln for ln in lines[i + 1:] if ln.strip()), "")
        if "$LASTEXITCODE" not in follow:
            unchecked.append(cmd)
    assert not unchecked, unchecked
    for step in REQUIRED_STOP:
        idx = next(i for i, ln in enumerate(lines) if f"aeos.exe {step}" in ln)
        assert "exit 1" in lines[idx + 1], f"'{step}' failure must stop the bootstrap"


def test_shell_bootstrap_stops_on_error():
    lines = _lines("bootstrap.sh")
    assert "set -euo pipefail" in lines
    for step in REQUIRED_STOP:
        line = next(ln for ln in lines if f"aeos {step}" in ln)
        assert "|| true" not in line, f"'{step}' failure must stop the bootstrap"


def test_ci_runs_both_bootstrap_scripts_including_a_failure_case():
    ci = (repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/bootstrap.sh --no-doctor" in ci and ".\\scripts\\bootstrap.ps1 -NoDoctor" in ci
    assert "bootstrap must fail" in ci  # negative case: an invalid data dir makes db upgrade fail


def _git_index_modes() -> dict[str, str]:
    root = repo_root()
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run([git, "ls-files", "-s", "-z"], cwd=root, capture_output=True, check=True).stdout
    modes: dict[str, str] = {}
    for entry in out.decode("utf-8").split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            modes[path] = meta.split()[0]
    return modes


def test_committed_shell_scripts_and_launchers_are_executable_in_git():
    """A fresh clone on macOS/Linux must be able to run ./scripts/bootstrap.sh (exit 126 otherwise).

    The mode comes from the git index (``git ls-files -s``), which is what a clone checks out - the
    Windows working tree has no executable bit, so the file system cannot be asked."""
    modes = _git_index_modes()
    root = repo_root()
    scripts = set()
    for path, mode in modes.items():
        if mode == "120000":
            continue  # symlink
        p = root / path
        if path.endswith(".sh") or (p.is_file() and p.read_bytes()[:2] == b"#!"):
            scripts.add(path)
    assert scripts >= POSIX_ENTRY_POINTS, POSIX_ENTRY_POINTS - scripts
    not_exec = {p: modes[p] for p in sorted(scripts) if modes[p] != "100755"}
    assert not not_exec, (
        f"committed without the executable bit (fix: git update-index --chmod=+x <path>): {not_exec}"
    )


def _git_for_windows_tool(name: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    for parent in Path(git).resolve().parents[:3]:
        candidate = parent / "bin" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _posix_shell(name: str) -> str | None:
    # On Windows only Git for Windows' MSYS shells qualify (System32\bash.exe is the WSL launcher).
    return shutil.which(name) if os.name != "nt" else _git_for_windows_tool(name)


_STUB_PY = """#!/bin/sh
echo "python $*" >> stub.log
exit 0
"""
_STUB_AEOS = """#!/bin/sh
echo "aeos $*" >> stub.log
if [ "$*" = "${STUB_FAIL:-}" ]; then exit 3; fi
exit 0
"""
# Prerequisite stubs: the version probe and the version print (bootstrap only calls -c here, because
# .venv/bin/python already exists, so no venv is created).
_STUB_SYS_PY = """#!/bin/sh
case "$*" in *print*) echo 3.13 ;; esac
exit 0
"""
_STUB_GIT = """#!/bin/sh
exit 0
"""


def _bootstrap_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A copy of bootstrap.sh next to stub venv tools that record each call in order (no network,
    no real pip/npm, nothing touches the repository)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(repo_root() / "scripts" / "bootstrap.sh", root / "scripts" / "bootstrap.sh")
    (root / "requirements.lock").write_text("", encoding="utf-8")
    (root / "aeos").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "scripts" / "dev.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)
    stubbin = tmp_path / "stubbin"
    stubbin.mkdir()
    for path, body in (
        (venv / "python", _STUB_PY),
        (venv / "aeos", _STUB_AEOS),
        (stubbin / "python3.13", _STUB_SYS_PY),
        (stubbin / "git", _STUB_GIT),
    ):
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = str(stubbin) + os.pathsep + env.get("PATH", "")
    env.pop("STUB_FAIL", None)
    return root, env


def _run_bootstrap(shell: str, root: Path, env: dict[str, str], *args: str) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        [shell, "scripts/bootstrap.sh", *args], cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    log = root / "stub.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.returncode, calls, proc.stdout + proc.stderr


EXPECTED_ORDER = [
    "python -m pip install --upgrade pip --quiet",
    "python -m pip install --quiet -r requirements.lock",
    "python -m pip install --quiet --no-deps -e .",
    "python -m pip check",
    "aeos ops backup create --label pre-bootstrap --if-exists",  # §07 step 0: back up before migrating
    "aeos db upgrade",
    "aeos seed",
    "aeos inventory reconcile",
    "python scripts/component_status.py",
]


@pytest.mark.parametrize("shell_name", ["bash", "sh"])
def test_shell_bootstrap_really_runs_its_steps_in_order(tmp_path, shell_name):
    """Runs scripts/bootstrap.sh for real under bash, and under ``sh`` (the documented fallback when the
    executable bit is lost; on Linux that is dash, so the script must re-run itself under bash)."""
    shell = _posix_shell(shell_name)
    if shell is None:
        pytest.skip(f"no POSIX {shell_name} available")
    root, env = _bootstrap_sandbox(tmp_path)
    code, calls, out = _run_bootstrap(shell, root, env, "--skip-frontend", "--no-doctor")
    assert code == 0, out
    assert calls == EXPECTED_ORDER, out
    assert "Next steps:" in out


@pytest.mark.parametrize(
    ("failing", "must_not_run"),
    [
        ("ops backup create --label pre-bootstrap --if-exists", "aeos db upgrade"),
        ("db upgrade", "aeos seed"),
        ("seed", "aeos inventory reconcile"),
    ],
)
def test_shell_bootstrap_really_stops_when_a_required_step_fails(tmp_path, failing, must_not_run):
    shell = _posix_shell("bash")
    if shell is None:
        pytest.skip("no POSIX bash available")
    root, env = _bootstrap_sandbox(tmp_path)
    env["STUB_FAIL"] = failing
    code, calls, out = _run_bootstrap(shell, root, env, "--skip-frontend", "--no-doctor")
    assert code != 0, out
    assert f"aeos {failing}" in calls, out
    assert must_not_run not in calls, out
    assert "Next steps:" not in out


def test_docs_give_a_fallback_when_the_executable_bit_is_lost():
    for doc in ("README.md", "docs/SETUP_MACOS.md"):
        text = (repo_root() / doc).read_text(encoding="utf-8")
        assert "sh scripts/bootstrap.sh" in text, doc
    for script in ("bootstrap.sh", "dev.sh"):
        lines = _lines(script)
        guard = next(i for i, ln in enumerate(lines) if 'exec bash "$0" "$@"' in ln)
        assert guard < lines.index("set -euo pipefail"), script
