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
    """A supervisor request. ``domain`` is the explicit human-selected PERSONAL/OFFICE domain and is
    required for every purpose except ``doctor.*`` probes; a ``project_id`` must name a registered
    project whose immutable domain equals ``domain``. Context files must come from that domain's
    data partition. The same preflight applies to the manual package (ADR-0040 addendum)."""

    purpose: str  # planning.primary|planning.challenge|planning.synthesis|planning.final|review.checkpoint|...
    role: str
    model: str  # alias or pinned id from policy (e.g. "opus", "claude-opus-5-5"); "fable" is refused (ADR-0131)
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


def approved_publisher() -> str | None:
    """The owner-pinned Claude Code publisher identity that applies on this OS (None = not pinned)."""
    from aeos.claude_bridge.binary import approved_publisher as impl

    return impl()


def binary_authenticity(path: Path) -> dict[str, Any]:
    """§12 "unmodified official binary": the authenticity view of the binary at *path* - the same OS code-signature
    gate the bridge applies before every native call (``gate`` OK/UNVERIFIED/DEGRADED/BLOCKED/NOT_CHECKED, reason,
    the pin that applies on this OS, a signature-specific remedy). Checked once per binary fingerprint."""
    from aeos.claude_bridge import binary as binmod

    sig = binmod.invocation_signature(path, binmod.fingerprint(path))
    pin = binmod.publisher_pin()
    return binmod.authenticity_view(sig, pin["signer"], pin_problem=binmod.pin_error(pin))


def publisher_pin() -> dict[str, Any]:
    """``{"signer", "status" PINNED|NOT_PINNED|UNREADABLE, "reason"}`` of the Claude Code publisher pin on this OS
    (UNREADABLE = the reviewed install-manifest cannot be read: the pin is unknown and the gate blocks)."""
    from aeos.claude_bridge.binary import publisher_pin as impl

    return impl()


def signature_remedy(*, pin_unreadable: bool = False) -> dict[str, Any]:
    """The signature-specific remedy (reinstall the official build, check the OS signature, change the pin only by
    a reviewed install-manifest PR) - never the /login remedy. With *pin_unreadable*: repair the install-manifest
    whose publisher pin cannot be read."""
    from aeos.claude_bridge.binary import MANIFEST_REMEDY, SIGNATURE_REMEDY

    return dict(MANIFEST_REMEDY if pin_unreadable else SIGNATURE_REMEDY)
