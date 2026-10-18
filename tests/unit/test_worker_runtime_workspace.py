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
        assert (ws.repo / ".git").is_dir()  # own .git, nothing shared
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
