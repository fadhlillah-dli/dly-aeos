"""Candidate build workspace (§09 self-build zones, §21): separate checkout, runtime commits,
platform candidates never share files with the running controller."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aeos.core.enums import Domain
from aeos.core.paths import DataLayout
from aeos.worker_runtime import workspace
from tests.unit.test_workforce_support import init_git_repo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("isolated", [False, True])
def test_workspace_diff_and_runtime_commit(tmp_path, isolated):
    src = tmp_path / "repo"
    base = init_git_repo(src, {"src/a.py": "a = 1\n", "README.md": "x\n"})
    layout = DataLayout(tmp_path / "data")
    ws = workspace.create_workspace(layout, domain=Domain.PERSONAL, project_key="proj", run_id="run-0001-aaaa",
                                    task_key="Task A", source_repo=src, base_commit=None, isolated=isolated)
    assert ws.base_commit == base and ws.branch.startswith("aeos/task-a-")
    assert ws.repo.is_dir() and str(ws.repo).startswith(str(layout.task_data(Domain.PERSONAL, "proj")))
    if isolated:
        assert _git(ws.repo, "remote") == ""  # no path back to the source repository
        # own git dir, outside the worker-editable tree; repo/.git is only a pointer file (ADR-0054)
        assert (ws.repo / ".git").is_file()
        assert ws.git_dir == ws.common_dir == (ws.root / "git").resolve()
        assert ws.repo.resolve() not in ws.git_dir.parents
        assert src.resolve() not in ws.git_dir.parents
    else:
        assert ws.common_dir == (src / ".git").resolve()
    (ws.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (ws.repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    ch = workspace.collect_changes(ws)
    assert {c.path for c in ch["changes"]} == {"src/a.py", "src/b.py"} and ch["added"] >= 2
    head = workspace.commit_candidate(ws, "aeos: candidate")
    assert head and head != base
    assert _git(ws.repo, "log", "-1", "--format=%an <%ae>") == "AEOS Runtime <aeos-runtime@localhost>"
    assert _git(ws.repo, "rev-parse", "--abbrev-ref", "HEAD") == ws.branch
    assert _git(src, "rev-parse", "main") == base  # source main untouched
    assert workspace.assert_not_loaded(ws.root) == []
    workspace.cleanup_workspace(ws)
    assert not ws.repo.exists()


def test_candidate_never_inside_controller_checkout(monkeypatch, tmp_path):
    src = tmp_path / "repo"
    init_git_repo(src)
    monkeypatch.setattr(workspace, "repo_root", lambda: tmp_path)
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                   run_id="run-x", task_key="t", source_repo=src, base_commit=None, isolated=True)


def test_rejects_bad_base_and_reused_run(tmp_path):
    src = tmp_path / "repo"
    init_git_repo(src)
    layout = DataLayout(tmp_path / "data")
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(layout, domain=Domain.PERSONAL, project_key="p", run_id="run-y", task_key="t",
                                   source_repo=src, base_commit="HEAD~1; rm -rf /", isolated=False)
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(layout, domain=Domain.PERSONAL, project_key="p", run_id="run-y", task_key="t",
                                   source_repo=src, base_commit=None, isolated=False)


def test_symlink_escape_detected(tmp_path):
    import os

    src = tmp_path / "repo"
    init_git_repo(src)
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                    run_id="run-z", task_key="t", source_repo=src, base_commit=None, isolated=True)
    try:
        os.symlink(str(tmp_path / "outside.txt"), str(ws.repo / "escape"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    ch = workspace.collect_changes(ws)
    assert "escape" in ch["symlinks_escaping"]


# ----------------------------------------------------------------------------- runtime git hardening (ADR-0054)
def _planted_fsmonitor(marker: Path) -> str:
    """A core.fsmonitor command that would create *marker* if git ever ran it."""
    import sys

    return f"\"{Path(sys.executable).as_posix()}\" -c \"open(r'{marker.as_posix()}', 'w').write('ran')\""


@pytest.mark.parametrize("isolated", [False, True])
def test_planted_git_config_is_refused_and_restored(tmp_path, isolated):
    """§50 'Candidate mengubah aktif policy/verifier/controller' / §09: a worker that plants an
    executable git setting (core.fsmonitor) in the candidate's git config never gets it executed by
    the runtime; the run is refused and the original config is restored."""
    src = tmp_path / "repo"
    init_git_repo(src, {"src/a.py": "a = 1\n"})
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                    run_id="run-cfg1", task_key="t", source_repo=src, base_commit=None,
                                    isolated=isolated)
    cfg = ws.common_dir / "config"
    original = cfg.read_bytes()
    marker = tmp_path / "fsmonitor-ran.txt"
    subprocess.run(["git", "config", "--file", str(cfg), "core.fsmonitor", _planted_fsmonitor(marker)], check=True)
    (ws.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    with pytest.raises(workspace.GitControlTampered) as exc:
        workspace.collect_changes(ws)
    assert any("config" in c for c in exc.value.changed)
    assert exc.value.restored  # original content put back
    assert cfg.read_bytes() == original
    assert not marker.exists()  # the planted command never ran
    with pytest.raises(workspace.GitControlTampered):
        workspace.commit_candidate(ws, "x")  # still refused: nothing is committed for a tampered run
    assert not marker.exists()


def test_branch_tracking_config_does_not_trip_the_guard(tmp_path):
    src = tmp_path / "repo"
    init_git_repo(src)
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                    run_id="run-cfg2", task_key="t", source_repo=src, base_commit=None, isolated=True)
    subprocess.run(["git", "config", "--file", str(ws.common_dir / "config"), f"branch.{ws.branch}.remote", "origin"],
                   check=True)
    (ws.repo / "new.txt").write_text("n\n", encoding="utf-8")
    assert workspace.collect_changes(ws)["files"] == 1  # owner actions like `push -u` stay allowed


def test_hook_planted_in_runtime_hooks_dir_is_refused(tmp_path):
    src = tmp_path / "repo"
    init_git_repo(src)
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                    run_id="run-hook", task_key="t", source_repo=src, base_commit=None, isolated=True)
    marker = tmp_path / "hook-ran.txt"
    (ws.hooks_dir / "post-commit").write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    (ws.repo / "new.txt").write_text("n\n", encoding="utf-8")
    with pytest.raises(workspace.GitControlTampered) as exc:
        workspace.collect_changes(ws)
    assert "nohooks/post-commit" in exc.value.changed
    assert not (ws.hooks_dir / "post-commit").exists() and not marker.exists()


def test_rewritten_git_pointer_is_ignored(tmp_path):
    """The candidate's ``.git`` pointer is worker-editable: the runtime keeps using the trusted git
    dir resolved at creation, so a redirect to another repository changes nothing there."""
    src = tmp_path / "repo"
    base = init_git_repo(src, {"src/a.py": "a = 1\n"})
    other = tmp_path / "other"
    other_head = init_git_repo(other, {"x.txt": "x\n"})
    ws = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="p",
                                    run_id="run-ptr", task_key="t", source_repo=src, base_commit=None, isolated=True)
    # The pointer file is created hidden on Windows (cannot be re-created with mode "w"): rewrite in place.
    with open(ws.repo / ".git", "r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.truncate()
        fh.write(f"gitdir: {(other / '.git').as_posix()}\n")
    (ws.repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
    ch = workspace.collect_changes(ws)
    assert "src/a.py" in {c.path for c in ch["changes"]}
    head = workspace.commit_candidate(ws, "aeos: candidate")
    trusted = subprocess.run(["git", "--git-dir", str(ws.git_dir), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
    assert head == trusted and head != base
    assert _git(other, "rev-parse", "HEAD") == other_head  # the redirect target is untouched


def test_runtime_git_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "z" * 40)
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    env = workspace._git_env()
    assert "OPENROUTER_API_KEY" not in env and "GIT_DIR" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
