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
    # additive (integration 2026-10-18): accepted/observed residual risks that stay true even when READY,
    # e.g. WEAK process sandbox, branch protection NOT_ENFORCEABLE. Never a reason to show green.
    residual_risks: list[str] = field(default_factory=list)


def capability_state(
    session: Session, capability: Capability, *, domain: str | None = None, project_id: str | None = None,
    exclude_checks: tuple[str, ...] = (),
) -> CapabilityState:
    """``exclude_checks`` (additive) evaluates the gate without the named checks — only for a caller that is
    itself the excluded prerequisite (e.g. enabling the implementer agent asks CAN_RUN_WORKER without
    ``worker.agent_definition``); every other check still applies."""
    from aeos.doctor.capabilities import capability_state as impl

    if exclude_checks:
        return impl(session, capability, domain=domain, project_id=project_id, exclude_checks=tuple(exclude_checks))
    return impl(session, capability, domain=domain, project_id=project_id)  # original call shape (stand-ins)


def require_capability(
    session: Session, capability: Capability, *, domain: str | None = None, project_id: str | None = None,
    exclude_checks: tuple[str, ...] = (),
) -> CapabilityState:
    """Raise ``Blocked`` (with reasons) unless the capability is READY."""
    from aeos.core.errors import Blocked

    state = (capability_state(session, capability, domain=domain, project_id=project_id,
                              exclude_checks=tuple(exclude_checks)) if exclude_checks
             else capability_state(session, capability, domain=domain, project_id=project_id))
    if state.status != "READY":
        raise Blocked(f"{capability.value} is {state.status}", code="CAPABILITY_NOT_READY",
                      details={"capability": capability.value, "reasons": state.reasons, "remedies": state.remedies,
                               "residual_risks": list(getattr(state, "residual_risks", []) or [])})
    return state


def resource_budget(session: Session) -> dict[str, Any] | None:
    """Additive (2026-10-19): the owner's hardware/OS resource budget (cpu_cores, ram_gib, disk_gib,
    max_parallel_workers) set with ``aeos doctor budget``; None while not set (UNKNOWN, never guessed)."""
    from aeos.doctor.checks.host import resource_budget as impl

    return impl(session)


def invalidate(session: Session, *, invalidator: str, reason: str = "") -> int:
    """Mark checks depending on *invalidator* (e.g. ``binary:claude``, ``config:routes``) as STALE."""
    from aeos.doctor.capabilities import invalidate as impl

    return impl(session, invalidator=invalidator, reason=reason)
