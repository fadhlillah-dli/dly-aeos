"""Trusted supervisor workspace (§12): one FRESH directory per request under
``DataLayout.supervisor_workspace/<request-id>``.

Never a repository, a worker sandbox or a project task-data directory: in non-interactive mode the
official client skips the trust dialog and would load hooks/MCP/settings/CLAUDE.md of a project
directory (S05), so the supervisor never runs from a folder a worker or repository controls.
Context files are copied in read-only; the prompt and context are refused when they contain a
known secret value or a high-confidence credential shape (U-13: no secrets in model prompts).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from aeos.claude_bridge.parsing import redact_strings
from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.hashing import file_sha256, sha256_text
from aeos.core.paths import DataLayout, UnsafePathError, repo_root, safe_segment

MAX_CONTEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_TOTAL_BYTES = 10 * 1024 * 1024
MAX_CONTEXT_FILES = 50


class WorkspaceError(RuntimeError):
    def __init__(self, message: str, code: str = "WORKSPACE_UNSAFE"):
        super().__init__(message)
        self.code = code


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _inside_git_repo(path: Path) -> Path | None:
    for p in [path, *path.parents]:
        if (p / ".git").exists():
            return p
    return None


def assert_workspace_safe(layout: DataLayout, workspace: Path) -> None:
    """Raise unless *workspace* is a supervisor directory isolated from repos and worker data."""
    base = layout.supervisor_workspace
    if not _within(workspace, base) or workspace.resolve() == base.resolve():
        raise WorkspaceError("supervisor workspace must be a request directory under the supervisor root")
    platform_repo = repo_root()
    if _within(workspace, platform_repo) or _within(platform_repo, workspace):
        raise WorkspaceError("supervisor workspace overlaps the platform repository")
    for domain in Domain:
        if _within(workspace, layout.domain_root(domain)):
            raise WorkspaceError("supervisor workspace is inside a domain/project/task-data directory")
    repo = _inside_git_repo(workspace)
    if repo is not None:
        raise WorkspaceError(f"supervisor workspace is inside a git repository ({repo.name}); "
                             "move the AEOS data directory out of any repository")


def prepare_workspace(settings: Settings, request_id: str) -> Path:
    layout = settings.layout
    layout.supervisor_workspace.mkdir(parents=True, exist_ok=True)
    try:
        ws = layout.supervisor_workspace / safe_segment(request_id)
    except UnsafePathError as exc:
        raise WorkspaceError(str(exc)) from exc
    assert_workspace_safe(layout, ws)
    if ws.exists():
        raise WorkspaceError("supervisor workspace already exists (requests never reuse a directory)")
    ws.mkdir(parents=True)
    return ws


def high_confidence_secret_hits(text: str) -> int:
    """Count registered secret values and token-shaped credentials in *text*.

    The generic ``key=value`` redaction rule is deliberately not used here: prompts legitimately
    contain phrases such as ``token: string`` and must not be refused for them.
    """
    if not text:
        return 0
    from aeos.core import redaction as r  # private access: see shared change request

    with r._lock:
        values = list(r._known_values)
    hits = sum(1 for v in values if v and v in text)
    hits += sum(1 for p in r._PATTERNS if not p.groups and p.search(text))
    return hits


def copy_context(ws: Path, files: list[Path]) -> list[dict[str, Any]]:
    """Copy context files read-only into ``<ws>/context``. Returns a manifest (name, size, hash)."""
    if len(files) > MAX_CONTEXT_FILES:
        raise WorkspaceError(f"too many context files ({len(files)} > {MAX_CONTEXT_FILES})", code="CONTEXT_TOO_LARGE")
    target = ws / "context"
    target.mkdir()
    manifest: list[dict[str, Any]] = []
    total = 0
    used: set[str] = set()
    for src in files:
        src = Path(src)
        if not src.is_file():
            raise WorkspaceError(f"context file not found: {src.name}", code="CONTEXT_MISSING")
        size = src.stat().st_size
        total += size
        if size > MAX_CONTEXT_FILE_BYTES or total > MAX_CONTEXT_TOTAL_BYTES:
            raise WorkspaceError("context exceeds the size limit", code="CONTEXT_TOO_LARGE")
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = ""
        if high_confidence_secret_hits(text):
            raise WorkspaceError(f"context file {src.name} contains a credential; refusing to send it",
                                 code="SECRET_IN_PROMPT")
        name = src.name if src.name not in used else f"{len(used)}-{src.name}"
        try:
            name = safe_segment(name)
        except UnsafePathError:
            name = f"context-{len(used)}{src.suffix if safe_suffix(src.suffix) else ''}"
        used.add(name)
        dest = target / name
        shutil.copyfile(src, dest)
        os.chmod(dest, stat.S_IREAD)
        manifest.append({"name": name, "bytes": size, "sha256": file_sha256(dest)})
    return manifest


def safe_suffix(suffix: str) -> bool:
    return bool(suffix) and len(suffix) <= 10 and suffix[1:].isalnum()


def cleanup_context(ws: Path) -> None:
    """Remove copied context after the run (the trace stays); read-only files need a chmod first."""
    target = ws / "context"
    if not target.exists():
        return
    for p in target.iterdir():
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass

    def _retry(func: Any, path: str, _exc: Any) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_retry)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(target, onerror=_retry)


def sanitized_argv(argv: list[str], schema_json: str) -> list[str]:
    """argv for the trace: the schema body is replaced by its hash."""
    return [f"<schema {sha256_text(schema_json)[:16]}>" if a == schema_json else a for a in argv]


def write_trace(ws: Path, layout: DataLayout, trace: dict[str, Any]) -> str:
    """Persist a redacted trace and return its reference relative to the data dir."""
    path = ws / "trace.json"
    path.write_text(json.dumps(redact_strings(trace), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path.relative_to(layout.root).as_posix()
