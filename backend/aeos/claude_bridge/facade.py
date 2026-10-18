"""Cross-module contract for the ClaudeNativeBridge (§12). Owned by ``aeos.claude_bridge``.

The bridge only uses the unmodified official Claude Code binary with the user's native login
(Claude Max). It never uses ``--bare`` (which bypasses subscription login), never runs from a
worker/repository directory, scrubs ANTHROPIC_API_KEY and other provider variables from the child
environment, and never touches Claude OAuth/session tokens. If the bridge is not proven, callers
fall back to a manual package the user runs in official Claude Code — never to the Claude API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ClaudeRequest:
    purpose: str  # planning.primary|planning.challenge|planning.synthesis|planning.final|review.checkpoint|...
    role: str
    model: str  # alias or pinned id from policy (e.g. "opus", "fable")
    prompt: str
    json_schema: dict[str, Any]
    project_id: str | None = None
    domain: str | None = None
    context_files: list[Path] = field(default_factory=list)  # copied read-only into the supervisor workspace
    timeout_seconds: int = 900
    effort: str | None = None
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])


@dataclass
class ClaudeResult:
    ok: bool
    structured: dict[str, Any] | None
    text: str
    model_requested: str
    model_resolved: str | None
    engine_version: str | None
    session_id: str | None
    usage: dict[str, Any]
    duration_ms: int | None
    error_code: str | None = None
    error: str = ""
    quota_exhausted: bool = False
    trace_ref: str | None = None
    mode: str = "NATIVE"  # NATIVE | MANUAL_FALLBACK | MOCK


def run_structured(request: ClaudeRequest) -> ClaudeResult:
    from aeos.claude_bridge.bridge import run_structured as impl

    return impl(request)


def capability_test() -> dict[str, Any]:
    """Probe binary/version/auth mode/model resolution with a tiny structured call."""
    from aeos.claude_bridge.bridge import capability_test as impl

    return impl()


def write_manual_package(request: ClaudeRequest, *, package_id: str) -> Path:
    """§12 fallback: write prompt+schema+context so the user can run it in official Claude Code."""
    from aeos.claude_bridge.manual import write_manual_package as impl

    return impl(request, package_id=package_id)


def read_manual_result(package_id: str) -> ClaudeResult | None:
    from aeos.claude_bridge.manual import read_manual_result as impl

    return impl(package_id)
