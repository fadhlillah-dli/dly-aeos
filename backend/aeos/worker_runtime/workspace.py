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
  into the controller process. Its git directory lives in ``<run>/git`` (``--separate-git-dir``),
  outside the tree the worker edits.

The runtime — never the worker — stages and commits the candidate change on the task branch.

Runtime git hardening (ADR-0054): the candidate tree is untrusted, so the runtime never lets it
choose which git directory or configuration is used. Every runtime git command on the candidate
passes the trusted ``--git-dir``/``--work-tree`` resolved at creation (a rewritten ``.git`` pointer
file is ignored), disables hooks and fsmonitor, never runs external diff/textconv drivers and gets
an environment without secret-looking variables. The git control files (config, worktree config,
info/attributes) are fingerprinted and backed up at creation and the runtime hooks directory must
stay empty; if anything changed during the run, the runtime refuses to run git on the candidate at
all (a planted ``core.fsmonitor``/filter driver/hook would otherwise execute in the controller's
context), restores the original control files (for the ``worktree`` strategy that is the owner's
own repository config) and the run is quarantined.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
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


class GitControlTampered(WorkspaceError):
    """The candidate's git control files changed during the run; runtime git is refused."""

    def __init__(self, changed: list[str], restored: list[str] | None = None):
        super().__init__("git control files changed during the run; runtime refused to run git on the "
                         f"candidate: {', '.join(changed)}")
        self.changed = changed
        self.restored = restored or []


def _git_env() -> dict[str, str]:
    """Environment for runtime git: no ``GIT_*`` overrides and no secret-looking variables (git on
    a candidate never needs the controller's credentials)."""
    from aeos.worker_runtime.processes import is_secret_like

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not is_secret_like(k, v)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def git(args: list[str], *, cwd: Path, timeout: int = GIT_TIMEOUT, check: bool = True,
        hooks_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if sys.platform == "win32":
        # Candidate workspaces live deep in the data dir; git refuses paths over MAX_PATH unless asked.
        cmd += ["-c", "core.longpaths=true"]
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
    git_dir: Path | None = None  # trusted git dir (resolved at creation, never re-discovered)
    common_dir: Path | None = None  # trusted common dir (refs/objects/config)
    control_snapshot: dict[str, str | None] = field(default_factory=dict)
    control_backup: dict[str, bytes | None] = field(default_factory=dict, repr=False)
    tampered: list[str] = field(default_factory=list)  # sticky: once set, runtime git is never run again

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "root": str(self.root), "repo": str(self.repo), "branch": self.branch,
                "base_commit": self.base_commit, "strategy": self.strategy,
                "git_dir": str(self.git_dir) if self.git_dir else None}


# ----------------------------------------------------------------------------- git control files
def _control_paths(git_dir: Path, common_dir: Path) -> list[Path]:
    paths = [git_dir / "config", git_dir / "config.worktree", common_dir / "config",
             common_dir / "config.worktree", common_dir / "info" / "attributes"]
    out: list[Path] = []
    for p in paths:
        if p not in out:
            out.append(p)
    return out


def _fingerprint(path: Path) -> str | None:
    """Hash of the security-relevant content of a git control file (None = absent).

    Config files are read with ``git config --file`` (reading never executes anything) and every
    entry except ``branch.*`` is fingerprinted, so ordinary owner actions such as ``push -u`` do not
    trip the check while any planted fsmonitor/filter/diff/include/hooks entry does. Unparsable
    config is fingerprinted byte-for-byte."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    if path.name not in ("config", "config.worktree"):
        return "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        proc = subprocess.run(["git", "config", "--file", str(path), "--list", "-z"], capture_output=True,
                              timeout=30, env=_git_env(), cwd=str(path.parent), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "raw:" + hashlib.sha256(raw).hexdigest()
    if proc.returncode != 0:
        return "raw:" + hashlib.sha256(raw).hexdigest()
    entries = sorted(e for e in proc.stdout.split(b"\0") if e and not e.lower().startswith(b"branch."))
    return "cfg:" + hashlib.sha256(b"\0".join(entries)).hexdigest()


def snapshot_control_files(git_dir: Path, common_dir: Path) -> dict[str, str | None]:
    return {str(p): _fingerprint(p) for p in _control_paths(git_dir, common_dir)}


def backup_control_files(git_dir: Path, common_dir: Path) -> dict[str, bytes | None]:
    """Exact bytes of the control files at creation (trusted at that moment; files are small)."""
    return {str(p): (p.read_bytes() if p.is_file() else None) for p in _control_paths(git_dir, common_dir)}


def _restore(ws: Workspace, changed: list[str]) -> list[str]:
    """Put the creation-time content back so a planted config/attribute never survives the run
    (for the ``worktree`` strategy the common config is the owner's own repository config)."""
    restored: list[str] = []
    for p in changed:
        if p not in ws.control_backup:
            continue
        original = ws.control_backup[p]
        path = Path(p)
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            restored.append(_label(path, ws))
        except OSError:
            continue
    return restored


def _hooks_dir_entries(ws: Workspace) -> list[str]:
    try:
        return sorted(p.name for p in ws.hooks_dir.iterdir())
    except OSError:
        return []


def verify_control_files(ws: Workspace) -> None:
    """Raise ``GitControlTampered`` when a git control file changed since creation or something was
    placed in the runtime's (empty) hooks directory. Changed control files are restored first."""
    if ws.git_dir is None or ws.common_dir is None:
        raise WorkspaceError("workspace has no trusted git directory")
    if ws.tampered:
        raise GitControlTampered(list(ws.tampered))
    current = snapshot_control_files(ws.git_dir, ws.common_dir)
    changed = sorted(p for p in set(current) | set(ws.control_snapshot)
                     if current.get(p) != ws.control_snapshot.get(p))
    hooks = _hooks_dir_entries(ws)
    if changed or hooks:
        restored = _restore(ws, changed)
        for name in hooks:
            target = ws.hooks_dir / name
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
                restored.append(f"nohooks/{name} (removed)")
            except OSError:
                continue
        ws.tampered = [*(_label(Path(p), ws) for p in changed), *(f"nohooks/{n}" for n in hooks)]
        raise GitControlTampered(list(ws.tampered), restored=restored)


def _label(p: Path, ws: Workspace) -> str:
    for base, name in ((ws.git_dir, "git-dir"), (ws.common_dir, "common-dir")):
        if base is None:
            continue
        try:
            return f"{name}/{p.relative_to(base).as_posix()}"
        except ValueError:
            continue
    return p.name


def cand_git(ws: Workspace, args: list[str], *, check: bool = True,
             timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """git on the untrusted candidate: trusted git dir, no hooks, no fsmonitor."""
    if ws.git_dir is None:
        raise WorkspaceError("workspace has no trusted git directory")
    return git(["--git-dir", ws.git_dir.as_posix(), "--work-tree", ws.repo.as_posix(),
                "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
               cwd=ws.repo, hooks_dir=ws.hooks_dir, check=check, timeout=timeout)


def _resolve_git_dirs(repo_dir: Path, hooks: Path) -> tuple[Path, Path]:
    """Resolve the git dir and common dir of a freshly created checkout (trusted at this moment)."""
    gd = git(["rev-parse", "--absolute-git-dir"], cwd=repo_dir, hooks_dir=hooks).stdout.strip()
    git_dir = Path(gd)
    proc = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo_dir, hooks_dir=hooks,
               check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        common = Path(proc.stdout.strip())
    else:  # git < 2.31: read the commondir pointer of a linked worktree
        pointer = git_dir / "commondir"
        common = (git_dir / pointer.read_text(encoding="utf-8").strip()).resolve() if pointer.exists() else git_dir
    return git_dir.resolve(), common.resolve()


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
        # The git directory is kept outside the worker-editable tree (``repo/.git`` is only a
        # pointer file the runtime never follows) and shares nothing with the controller.
        sep = root / "git"
        git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", f"--separate-git-dir={sep.as_posix()}",
             source_repo.as_posix(), repo_dir.as_posix()], cwd=root, hooks_dir=hooks, timeout=600)
        git(["--git-dir", sep.as_posix(), "remote", "remove", "origin"], cwd=root, hooks_dir=hooks)
        git(["--git-dir", sep.as_posix(), "--work-tree", repo_dir.as_posix(), "-c", "core.fsmonitor=false",
             "checkout", "--quiet", "-b", branch, base], cwd=repo_dir, hooks_dir=hooks)
        strategy = "isolated-clone"
        git_dir = common_dir = sep.resolve()
    else:
        git(["worktree", "add", "--quiet", "-b", branch, repo_dir.as_posix(), base], cwd=source_repo, hooks_dir=hooks)
        strategy = "worktree"
        git_dir, common_dir = _resolve_git_dirs(repo_dir, hooks)
    return Workspace(run_id=run_id, root=root, repo=repo_dir, sandbox_root=root, hooks_dir=hooks, branch=branch,
                     base_commit=base, strategy=strategy, source_repo=source_repo, git_dir=git_dir,
                     common_dir=common_dir, control_snapshot=snapshot_control_files(git_dir, common_dir),
                     control_backup=backup_control_files(git_dir, common_dir))


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
    """Stage everything and diff against the base commit (runtime-observed, not worker-claimed).

    Refuses (``GitControlTampered``) before running any git command when the candidate's git
    control files changed during the run (ADR-0054)."""
    verify_control_files(ws)
    cand_git(ws, ["add", "-A", "--", "."])
    diff = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
    changes = _parse_name_status(cand_git(ws, [*diff, "--name-status", "-z", "-M", ws.base_commit]).stdout)
    raw = cand_git(ws, [*diff, "--raw", "-z", "--no-renames", ws.base_commit]).stdout
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
    numstat = cand_git(ws, [*diff, "--numstat", ws.base_commit]).stdout
    added = deleted = 0
    for line in numstat.splitlines():
        a, d, *_ = [*line.split("\t"), "", ""][:3]
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    patch = cand_git(ws, [*diff, "--binary", ws.base_commit]).stdout
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


RUNTIME_SOURCE = "runtime-default (not linked to an account)"


def commit_identity(name: str | None, email: str | None) -> tuple[str, str, str]:
    """(name, email, source) for candidate commits (§06 "legitimate identity ... linked to the user's account
    or noreply address"). A valid owner identity (``aeos.core.commit_identity.validate_identity``: no AI/bot
    identity) is used as-is; otherwise the runtime identity, reported as NOT linked (and never pushable)."""
    from aeos.core.commit_identity import validate_identity

    if name and email and validate_identity(name, email) is None:
        return name.strip(), email.strip(), "owner-configured"
    return RUNTIME_IDENTITY[0], RUNTIME_IDENTITY[1], RUNTIME_SOURCE


def commit_candidate(ws: Workspace, message: str, *, identity: tuple[str, str] | None = None,
                     trailers: dict[str, str] | None = None) -> str | None:
    """Commit the staged candidate on the task branch; returns head sha. Author and committer are the owner's
    identity for the project's code host (or the runtime identity, never pushable). Commits can reach a code host,
    so the message carries no AI attribution and no agent/route/model trailers (owner rule, AGENTS.md invariant
    11): that provenance stays in the run record. A message that would carry it is refused."""
    from aeos.core.attribution import find_ai_attribution

    verify_control_files(ws)
    status = cand_git(ws, ["diff", "--cached", "--quiet", "--no-ext-diff"], check=False)
    if status.returncode == 0:
        return cand_git(ws, ["rev-parse", "HEAD"]).stdout.strip() or None
    name, email = identity or RUNTIME_IDENTITY
    lines = [f"{k}: {str(v).splitlines()[0][:200]}" for k, v in (trailers or {}).items() if v]
    if lines:
        message = message.rstrip() + "\n\n" + "\n".join(lines)
    findings = find_ai_attribution(message)
    if findings:
        raise WorkspaceError("candidate commit message carries AI attribution (refused, owner rule): "
                             + "; ".join(f.describe() for f in findings[:3]))
    cand_git(ws, ["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
                  "commit", "--quiet", "--no-verify", "-m", message])
    return cand_git(ws, ["rev-parse", "HEAD"]).stdout.strip()


MAX_CANDIDATE_COMMITS = 50


def candidate_commits(ws: Workspace, head: str) -> list[Any]:
    """The commits ``base..head`` of the candidate (the runtime's commit plus any the worker made itself), read
    through the trusted git dir as ``aeos.core.commit_identity.CommitRecord`` - what the broker will check before
    a push. More than ``MAX_CANDIDATE_COMMITS`` raises (the caller marks the candidate not pushable)."""
    from aeos.core.commit_identity import parse_commit_object, valid_sha

    verify_control_files(ws)
    shas = cand_git(ws, ["rev-list", f"--max-count={MAX_CANDIDATE_COMMITS + 1}", head, "--not",
                         ws.base_commit]).stdout.split()
    if len(shas) > MAX_CANDIDATE_COMMITS:
        raise WorkspaceError(f"more than {MAX_CANDIDATE_COMMITS} commits on the task branch")
    out = []
    for sha in shas:
        if not valid_sha(sha):
            raise WorkspaceError("unexpected rev-list output")
        out.append(parse_commit_object(sha, cand_git(ws, ["cat-file", "commit", sha]).stdout))
    return out


def agent_config_files(repo: Path) -> list[str]:
    """Repository-level OpenCode configuration present in the worktree (it would be loaded as project config)."""
    from aeos.worker_runtime.opencode import PROJECT_CONFIG_FILES

    return [n for n in PROJECT_CONFIG_FILES if os.path.lexists(repo / n)]


# §19/§21 structured worker message: the worker writes it inside its worktree; the runtime takes it out of
# the candidate before computing the diff. It is untrusted DATA (validated against a schema, size-capped,
# never followed through a symlink) and can only open a decision request / stop for the owner.
REPORT_PATH = ".aeos/report.json"
MAX_REPORT_BYTES = 64 * 1024


def take_worker_report(ws: Workspace) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(parsed JSON or None, info). Removes an untracked report file from the candidate."""
    info: dict[str, Any] = {"path": REPORT_PATH, "present": False}
    parent = ws.repo / ".aeos"
    target = parent / "report.json"
    if os.path.islink(parent):
        # Never follow (or delete through) a symlinked directory; the symlink itself stays in the candidate
        # diff, where the scope check flags it.
        info.update(present=True, rejected="report directory is a symlink (refused, left in the candidate)")
        return None, info
    if not os.path.lexists(target):
        return None, info
    info["present"] = True
    verify_control_files(ws)
    tracked = cand_git(ws, ["ls-files", "--error-unmatch", "--", REPORT_PATH], check=False).returncode == 0
    if tracked:
        info["ignored"] = "report path is tracked in the base commit; not a worker report"
        return None, info
    data: dict[str, Any] | None = None
    try:
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode):
            info["rejected"] = "report is not a regular file inside the worktree (symlink refused)"
        elif st.st_size > MAX_REPORT_BYTES:
            info["rejected"] = f"report larger than {MAX_REPORT_BYTES} bytes"
        else:
            ensure_within(ws.repo, target)
            raw = target.read_bytes()
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict):
                    data = parsed
                else:
                    info["rejected"] = "report is not a JSON object"
            except (UnicodeDecodeError, json.JSONDecodeError):
                info["rejected"] = "report is not valid UTF-8 JSON"
    except (OSError, ValueError) as exc:
        info["rejected"] = f"report unreadable ({type(exc).__name__})"
    try:
        target.unlink()  # a symlink is removed itself (unlink never follows it)
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        info["removed_from_candidate"] = True
    except OSError:
        info["removed_from_candidate"] = False
    return data, info


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
