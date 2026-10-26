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


def recheck(session: Session, *, checks: list[str], actor: str, trigger: str) -> dict[str, str]:
    """Additive (2026-10-25): re-run exactly the named checks now and persist their results in *session*
    (caller commits) - for an owner action that changes what one DB-only check observes (e.g. enabling the
    implementer agent re-checks ``worker.agent_definition``), so the gate does not keep a result made STALE by
    that action. Returns {check_id: status}. Never runs a functional or paid probe."""
    from aeos.core.config import get_settings
    from aeos.doctor.runner import run_doctor

    report = run_doctor(get_settings(), session, checks=list(checks), actor=actor, trigger=trigger)
    return {r.check_id: r.status for r in report.results}


def request_recheck(check_ids: list[str]) -> list[str]:
    """Additive (§08 "memicu recheck sebelum capability tersebut digunakan kembali"): queue a background recheck of
    the given checks that may be re-run implicitly (local non-functional checks and the zero-cost remote identity
    probes; never quota-bearing ones). Returns the ids queued - empty when no server recheck worker runs (CLI)."""
    from aeos.doctor import capabilities as caps

    if not caps.auto_recheck_enabled():
        return []
    ids = [*caps.recheckable(list(check_ids)), *caps.remote_recheckable(list(check_ids))]
    if ids:
        caps.request_recheck(ids)
    return ids


def launcher_command(args: str) -> str:
    """Additive: the copyable AEOS launcher command for this OS (``.\\aeos.ps1 <args>`` on Windows, ``./aeos <args>``
    on macOS), as the Doctor's own remedies print it."""
    from aeos.doctor.model import aeos_cmd

    return aeos_cmd(args)


def check_catalog() -> dict[str, Any]:
    """The registered Readiness Doctor checks (count and group labels) - static catalog, no results."""
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.model import GROUPS

    load_all()
    used = {s.group for s in REGISTRY.values()}
    return {"count": len(REGISTRY), "groups": [label for g, label in GROUPS if g in used]}
