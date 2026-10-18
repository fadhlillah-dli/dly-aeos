"""Candidate build workspace per run (§06, §09, §21).

Layout under ``DataLayout.task_data(domain, project_key)/<run_id>/``::

    repo/      candidate checkout the worker may edit (cwd of the worker)
    home/ tmp/ config/   sandbox HOME/TEMP and the per-run OpenCode config (outside repo/)
    nohooks/   empty hooks dir so no repository hook runs during runtime git operations

Strategies (ADR-0052):

* ``worktree`` (default for user projects): ``git worktree add -b aeos/<task>-<run8> <dir> <base>``.
* ``isolated-clone`` (always for the platform/self-build project, §09): a ``--no-hardlinks`` clone
  with its remote removed, so the candidate never shares files — not even ``.git`` hooks/refs/
  objects — with the running controller checkout. Nothing from the candidate is ever imported
  into the controller process.

The runtime — never the worker — stages and commits the candidate change on the task branch.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.core.enums import Domain
from aeos.core.ids import slugify
from aeos.core.paths import DataLayout, ensure_within, repo_root, safe_segment
from aeos.core.redaction import redact_text
from aeos.worker_runtime.scope import ChangedPath

GIT_TIMEOUT = 120
MAX_PATCH_BYTES = 5 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH_OK = re.compile(r"^aeos/[a-z0-9][a-z0-9._-]{0,80}$")
RUNTIME_IDENTITY = ("AEOS Runtime", "aeos-runtime@localhost")


class WorkspaceError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def git(args: list[str], *, cwd: Path, timeout: int = GIT_TIMEOUT, check: bool = True,
        hooks_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if hooks_dir is not None:
        cmd += ["-c", f"core.hooksPath={hooks_dir.as_posix()}"]
    cmd += args
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=_git_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git {args[0]} failed: {type(exc).__name__}") from exc
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed (exit {proc.returncode}): "
                             f"{redact_text(proc.stderr.strip())[:500]}")
    return proc


def task_branch_name(task_key: str, run_id: str) -> str:
    name = f"aeos/{slugify(task_key, max_len=60)}-{run_id.replace('-', '')[:8].lower()}"
    if not _BRANCH_OK.match(name):
        raise WorkspaceError(f"unsafe branch name {name!r}")
    return name


@dataclass
class Workspace:
    run_id: str
    root: Path
    repo: Path
    sandbox_root: Path
    hooks_dir: Path
    branch: str
    base_commit: str
    strategy: str  # worktree | isolated-clone
    source_repo: Path

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "root": str(self.root), "repo": str(self.repo), "branch": self.branch,
                "base_commit": self.base_commit, "strategy": self.strategy}


def _assert_separate_from_controller(path: Path) -> None:
    """§09: the candidate never lives inside (or contains) the running controller checkout."""
    controller = repo_root().resolve()
    cand = path.resolve()
    if cand == controller or controller in cand.parents or cand in controller.parents:
        raise WorkspaceError("candidate workspace must not share files with the running controller checkout")


def resolve_base(repo: Path, base_commit: str | None) -> str:
    ref = base_commit or "HEAD"
    if base_commit and not _SHA.match(base_commit):
        raise WorkspaceError("base_commit must be a commit sha")
    out = git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo).stdout.strip()
    if not _SHA.match(out):
        raise WorkspaceError("could not resolve base commit")
    return out


def create_workspace(
    layout: DataLayout, *, domain: Domain, project_key: str, run_id: str, task_key: str, source_repo: Path,
    base_commit: str | None, isolated: bool,
) -> Workspace:
    safe_segment(run_id)
    root = layout.task_data(domain, project_key) / run_id
    if root.exists():
        raise WorkspaceError(f"workspace for run {run_id} already exists")
    root.mkdir(parents=True)
    _assert_separate_from_controller(root)
    hooks = root / "nohooks"
    hooks.mkdir()
    repo_dir = root / "repo"
    source_repo = source_repo.resolve()
    base = resolve_base(source_repo, base_commit)
    branch = task_branch_name(task_key, run_id)
    if isolated:
        git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", source_repo.as_posix(), repo_dir.as_posix()],
            cwd=root, hooks_dir=hooks, timeout=600)
        git(["remote", "remove", "origin"], cwd=repo_dir, hooks_dir=hooks)
        git(["checkout", "--quiet", "-b", branch, base], cwd=repo_dir, hooks_dir=hooks)
        strategy = "isolated-clone"
    else:
        git(["worktree", "add", "--quiet", "-b", branch, repo_dir.as_posix(), base], cwd=source_repo, hooks_dir=hooks)
        strategy = "worktree"
    return Workspace(run_id=run_id, root=root, repo=repo_dir, sandbox_root=root, hooks_dir=hooks, branch=branch,
                     base_commit=base, strategy=strategy, source_repo=source_repo)


def _parse_name_status(z: str) -> list[ChangedPath]:
    items = z.split("\0")
    out: list[ChangedPath] = []
    i = 0
    while i < len(items):
        code = items[i]
        if not code:
            i += 1
            continue
        kind = code[0]
        if kind in ("R", "C"):
            out.append(ChangedPath(path=items[i + 2], old_path=items[i + 1], status=kind))
            i += 3
        else:
            out.append(ChangedPath(path=items[i + 1], status=kind))
            i += 2
    return out


def collect_changes(ws: Workspace) -> dict[str, Any]:
    """Stage everything and diff against the base commit (runtime-observed, not worker-claimed)."""
    git(["add", "-A", "--", "."], cwd=ws.repo, hooks_dir=ws.hooks_dir)
    changes = _parse_name_status(
        git(["diff", "--cached", "--name-status", "-z", "-M", ws.base_commit], cwd=ws.repo).stdout)
    raw = git(["diff", "--cached", "--raw", "-z", "--no-renames", ws.base_commit], cwd=ws.repo).stdout
    modes: dict[str, str] = {}
    parts = raw.split("\0")
    for j in range(0, len(parts) - 1, 2):
        meta, path = parts[j], parts[j + 1]
        if meta.startswith(":"):
            fields = meta[1:].split()
            if len(fields) >= 2:
                modes[path] = fields[1]
    for ch in changes:
        ch.mode = modes.get(ch.path)
    numstat = git(["diff", "--cached", "--numstat", ws.base_commit], cwd=ws.repo).stdout
    added = deleted = 0
    for line in numstat.splitlines():
        a, d, *_ = [*line.split("\t"), "", ""][:3]
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    patch = git(["diff", "--cached", "--binary", ws.base_commit], cwd=ws.repo).stdout
    truncated = len(patch.encode("utf-8", "replace")) > MAX_PATCH_BYTES
    if truncated:
        patch = patch.encode("utf-8", "replace")[:MAX_PATCH_BYTES].decode("utf-8", "ignore")
    escaping = []
    for ch in changes:
        if ch.mode == "120000" and ch.status != "D":
            target = ws.repo / ch.path
            try:
                ensure_within(ws.repo, target)
            except ValueError:
                escaping.append(ch.path)
    return {"changes": changes, "files": len(changes), "added": added, "deleted": deleted, "patch": patch,
            "patch_truncated": truncated, "symlinks_escaping": escaping}


def commit_candidate(ws: Workspace, message: str) -> str | None:
    """Commit the staged candidate on the task branch as the runtime identity; returns head sha."""
    status = git(["diff", "--cached", "--quiet"], cwd=ws.repo, check=False)
    if status.returncode == 0:
        return git(["rev-parse", "HEAD"], cwd=ws.repo).stdout.strip() or None
    name, email = RUNTIME_IDENTITY
    git(["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
         "commit", "--quiet", "--no-verify", "-m", message], cwd=ws.repo, hooks_dir=ws.hooks_dir)
    return git(["rev-parse", "HEAD"], cwd=ws.repo).stdout.strip()


def _onerror(func: Any, path: str, _exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def cleanup_workspace(ws: Workspace, *, keep_sandbox: bool = True) -> dict[str, Any]:
    """Remove the candidate checkout (evidence/patch are stored separately). Idempotent."""
    result: dict[str, Any] = {"repo_removed": False}
    if ws.strategy == "worktree":
        git(["worktree", "remove", "--force", ws.repo.as_posix()], cwd=ws.source_repo, check=False)
        git(["worktree", "prune"], cwd=ws.source_repo, check=False)
    if ws.repo.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(ws.repo, onexc=_onerror)
        else:  # pragma: no cover
            shutil.rmtree(ws.repo, onerror=_onerror)
    result["repo_removed"] = not ws.repo.exists()
    if not keep_sandbox and ws.root.exists():
        shutil.rmtree(ws.root, ignore_errors=True)
    return result


def assert_not_loaded(path: Path) -> list[str]:
    """§09: return controller-loaded modules whose file lives inside the candidate (must be empty)."""
    cand = path.resolve()
    hits = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            if cand in Path(f).resolve().parents:
                hits.append(name)
        except (OSError, ValueError):
            continue
    return hits
