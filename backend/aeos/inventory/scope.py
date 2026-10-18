"""Scoped, read-only readers for the managed repository (§37 coverage, §38 step 2).

Only named files inside the platform repository are read: dependency manifests and lockfiles,
CI workflows, VS Code recommendations and MCP/agent configuration files that a non-bare Claude
session or a worker engine would load (§12). Nothing outside the repository is scanned and nothing
is written. A file that is absent is reported as a coverage gap, never as "nothing there".
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.core.paths import UnsafePathError, ensure_within
from aeos.core.timeutil import utcnow

MAX_FILE_BYTES = 8 * 1024 * 1024
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*?)\s*(;.*)?$")
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MCP_CONFIG_FILES = (".mcp.json", ".claude/settings.json", ".vscode/mcp.json", "opencode.json", "opencode.jsonc")


@dataclass
class SourceRead:
    source: str  # logical path relative to the repo
    status: str  # READ | MISSING | ERROR
    entries: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: utcnow().isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "status": self.status, "entries": self.entries, "detail": self.detail,
                "at": self.at}


def _read_text(repo: Path, rel: str) -> str | None:
    path = repo / rel
    try:
        resolved = ensure_within(repo, path)  # symlinks pointing outside the repo are not followed
    except UnsafePathError:
        return None
    if not resolved.is_file() or resolved.stat().st_size > MAX_FILE_BYTES:
        return None
    return resolved.read_text(encoding="utf-8", errors="replace")


def parse_requirement(spec: str) -> tuple[str, str] | None:
    m = _REQ_RE.match(spec or "")
    if not m:
        return None
    return m.group(1).lower().replace("_", "-"), (m.group(3) or "").strip()


def read_pyproject(repo: Path) -> tuple[SourceRead, list[dict[str, Any]]]:
    rel = "pyproject.toml"
    text = _read_text(repo, rel)
    if text is None:
        return SourceRead(rel, "MISSING"), []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return SourceRead(rel, "ERROR", detail={"error": str(exc)[:200]}), []
    project = data.get("project") or {}
    out: list[dict[str, Any]] = []
    for spec in project.get("dependencies") or []:
        parsed = parse_requirement(spec)
        if parsed:
            out.append({"name": parsed[0], "spec": parsed[1], "group": "runtime"})
    for group, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs or []:
            parsed = parse_requirement(spec)
            if parsed:
                out.append({"name": parsed[0], "spec": parsed[1], "group": str(group)})
    return SourceRead(rel, "READ", len(out), {"project": project.get("name"), "version": project.get("version")}), out


def read_python_lock(repo: Path) -> SourceRead:
    rel = "requirements.lock"
    text = _read_text(repo, rel)
    if text is None:
        return SourceRead(rel, "MISSING", detail={"gap": "Lockfile Python belum ada; versi paket hanya dari lingkungan yang berjalan"})
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return SourceRead(rel, "READ", len(lines))


def read_package_lock(repo: Path) -> tuple[SourceRead, list[dict[str, Any]], int]:
    rel = "frontend/package-lock.json"
    text = _read_text(repo, rel)
    if text is None:
        return SourceRead(rel, "MISSING", detail={"gap": "lockfile frontend belum ada"}), [], 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return SourceRead(rel, "ERROR", detail={"error": str(exc)[:200]}), [], 0
    packages = data.get("packages") or {}
    root = packages.get("") or {}
    direct: list[dict[str, Any]] = []
    for group in ("dependencies", "devDependencies"):
        for name, spec in (root.get(group) or {}).items():
            resolved = (packages.get(f"node_modules/{name}") or {}).get("version")
            direct.append({"name": name, "spec": spec, "resolved": resolved,
                           "group": "runtime" if group == "dependencies" else "dev"})
    transitive = sum(1 for k in packages if k.startswith("node_modules/")) - len(direct)
    detail = {"lockfileVersion": data.get("lockfileVersion"), "transitive_packages": max(transitive, 0)}
    return SourceRead(rel, "READ", len(direct), detail), direct, max(transitive, 0)


def npm_installed_version(repo: Path, package: str) -> str | None:
    rel = f"frontend/node_modules/{package}/package.json"
    text = _read_text(repo, rel)
    if text is None:
        return None
    try:
        return str(json.loads(text).get("version") or "") or None
    except json.JSONDecodeError:
        return None


def read_workflows(repo: Path) -> tuple[SourceRead, list[dict[str, Any]]]:
    rel = ".github/workflows"
    wf_dir = repo / rel
    if not wf_dir.is_dir():
        return SourceRead(rel, "MISSING"), []
    actions: list[dict[str, Any]] = []
    files = sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")])
    for f in files:
        text = _read_text(repo, f"{rel}/{f.name}")
        if text is None:
            continue
        for uses in _USES_RE.findall(text):
            if uses.startswith("./"):
                continue
            name, _, ref = uses.partition("@")
            actions.append({"uses": name, "ref": ref or None, "pinned_sha": bool(_SHA_RE.match(ref or "")),
                            "file": f"{rel}/{f.name}"})
    return SourceRead(rel, "READ", len(actions), {"files": [f.name for f in files]}), actions


def read_vscode_extensions(repo: Path) -> tuple[SourceRead, list[str]]:
    rel = ".vscode/extensions.json"
    text = _read_text(repo, rel)
    if text is None:
        return SourceRead(rel, "MISSING"), []
    try:
        data = json.loads(_strip_jsonc(text))
    except json.JSONDecodeError as exc:
        return SourceRead(rel, "ERROR", detail={"error": str(exc)[:200]}), []
    recs = [str(x) for x in data.get("recommendations") or []]
    return SourceRead(rel, "READ", len(recs)), recs


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in text.splitlines())


def read_mcp_configs(repo: Path) -> tuple[list[SourceRead], list[dict[str, Any]]]:
    """MCP servers configured inside the repository (loaded by non-bare Claude / worker engines)."""
    reads: list[SourceRead] = []
    servers: list[dict[str, Any]] = []
    for rel in MCP_CONFIG_FILES:
        text = _read_text(repo, rel)
        if text is None:
            reads.append(SourceRead(rel, "MISSING"))
            continue
        try:
            data = json.loads(_strip_jsonc(text))
        except json.JSONDecodeError as exc:
            reads.append(SourceRead(rel, "ERROR", detail={"error": str(exc)[:200]}))
            continue
        block = data.get("mcpServers") or data.get("servers") or data.get("mcp") or {}
        names = list(block) if isinstance(block, dict) else []
        for name in names:
            cfg = block.get(name) or {}
            command = cfg.get("command") if isinstance(cfg, dict) else None
            if isinstance(command, list):
                command = command[0] if command else None
            servers.append({"name": str(name), "file": rel, "command": str(command)[:120] if command else None})
        hooks = bool(data.get("hooks")) if isinstance(data, dict) else False
        reads.append(SourceRead(rel, "READ", len(names), {"hooks_configured": hooks}))
    return reads, servers
