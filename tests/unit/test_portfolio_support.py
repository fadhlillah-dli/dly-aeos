"""Shared helpers for the Gateway/Portfolio, Intake and Knowledge tests (no tests in this module)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from aeos.core.enums import Domain

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git executable not available")


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=_git_env(), input=input_bytes,
                         timeout=60, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git {args} failed: {res.stderr.decode(errors='replace')}")
    return res.stdout.decode("utf-8", errors="replace").strip()


def make_repo(root: Path, files: dict[str, str | bytes], *, origin: str | None = None,
              symlinks: dict[str, str] | None = None) -> Path:
    """Create a git repo with one commit. ``symlinks`` adds mode-120000 tree entries without needing
    filesystem symlink support (works on Windows)."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    for k, v in (("user.email", "tests@example.invalid"), ("user.name", "AEOS Tests"), ("commit.gpgsign", "false"),
                 ("core.autocrlf", "false"), ("core.symlinks", "false")):
        git(root, "config", k, v)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_bytes(content.encode("utf-8"))
    git(root, "add", "-A")
    for link, target in (symlinks or {}).items():
        sha = git(root, "hash-object", "-w", "--stdin", input_bytes=target.encode())
        git(root, "update-index", "--add", "--cacheinfo", f"120000,{sha},{link}")
        # core.symlinks=false: the checkout form of a symlink is a plain file holding the target text
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        (root / link).write_bytes(target.encode())
    git(root, "commit", "-q", "-m", "initial")
    if origin:
        git(root, "remote", "add", "origin", origin)
    return root


def commit_file(repo: Path, rel: str, content: str, message: str = "change") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def create_project(client: Any, *, domain: Domain = Domain.PERSONAL, name: str = "Demo App", **extra: Any) -> dict:
    body: dict[str, Any] = {"domain": domain.value, "name": name, "idea": extra.pop("idea", "Aplikasi contoh")}
    body.update(extra)
    r = client.post("/api/projects", json=body)
    assert r.status_code == 200, r.text
    return r.json()["project"]


def install_fake_policy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-in for ``aeos.policy.decisions`` (built by another module) that records real Decision rows."""
    from aeos.models.decisions import Decision

    calls: list[dict[str, Any]] = []
    mod = types.ModuleType("aeos.policy.decisions")

    def record_decision(session: Any, **kw: Any) -> Decision:
        calls.append(kw)
        d = Decision(kind=kw["kind"], actor_user_id=kw["actor_user_id"], target_type=kw["target_type"],
                     target_id=kw["target_id"], target_version=kw.get("target_version"),
                     target_hash=kw.get("target_hash"), allowed_operations=kw.get("allowed_operations") or [],
                     auth_scope=kw.get("auth_scope") or {}, project_id=kw.get("project_id"), domain=kw.get("domain"),
                     rationale=kw.get("rationale") or "", details=kw.get("details") or {})
        session.add(d)
        session.flush()
        return d

    mod.record_decision = record_decision  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aeos.policy.decisions", mod)
    return calls


def block_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the decision registry being unavailable (import fails)."""
    monkeypatch.setitem(sys.modules, "aeos.policy.decisions", None)  # type: ignore[arg-type]
