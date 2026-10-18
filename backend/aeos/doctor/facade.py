"""Cross-module contract for capability readiness (§08). Owned by ``aeos.doctor``.

Readiness is evaluated per capability and target from code/probes — never from model narration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Capability


@dataclass(frozen=True)
class CapabilityState:
    capability: Capability
    status: str  # READY | BLOCKED | DEGRADED | UNKNOWN | NOT_APPLICABLE
    reasons: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)  # check_id/status/last_checked_at
    remedies: list[dict[str, Any]] = field(default_factory=list)


def capability_state(
    session: Session, capability: Capability, *, domain: str | None = None, project_id: str | None = None
) -> CapabilityState:
    from aeos.doctor.capabilities import capability_state as impl

    return impl(session, capability, domain=domain, project_id=project_id)


def require_capability(
    session: Session, capability: Capability, *, domain: str | None = None, project_id: str | None = None
) -> CapabilityState:
    """Raise ``Blocked`` (with reasons) unless the capability is READY."""
    from aeos.core.errors import Blocked

    state = capability_state(session, capability, domain=domain, project_id=project_id)
    if state.status != "READY":
        raise Blocked(f"{capability.value} is {state.status}", code="CAPABILITY_NOT_READY",
                      details={"capability": capability.value, "reasons": state.reasons, "remedies": state.remedies})
    return state


def invalidate(session: Session, *, invalidator: str, reason: str = "") -> int:
    """Mark checks depending on *invalidator* (e.g. ``binary:claude``, ``config:routes``) as STALE."""
    from aeos.doctor.capabilities import invalidate as impl

    return impl(session, invalidator=invalidator, reason=reason)
