"""Owner enable/disable of an implementation AgentDefinition (§17, §19, §35 Workforce Console).

One service for the Control Center (``POST /api/workforce/agents/{key}/enable|disable``) and the terminal
(``aeos work agent-enable|agent-disable``). An agent is enabled only when every prerequisite is really READY (no
fake activity): the engine for the sandbox that will run it (the host OpenCode binary for the process sandbox, the
pinned worker image for the container sandbox), a READY sandbox, an eligible APPROVED/TRIAL worker route and every
other CAN_RUN_WORKER check (the gate itself also requires this agent, so that one check is excluded here). Enabling
is not a run: the scheduler still admits nothing without an approved plan, budget, route and verifier.

Services never commit (callers commit).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import Capability
from aeos.core.errors import Blocked, NotFound
from aeos.core.redaction import redact_text
from aeos.models.workforce import AgentDefinition

log = logging.getLogger("aeos.workforce.agents")
AGENT_CHECK = "worker.agent_definition"


def get_agent(session: Session, agent_key: str) -> AgentDefinition:
    agent = session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    if agent is None:
        raise NotFound("agent not found")
    return agent


def _engine_reasons(sandbox: dict[str, Any]) -> list[str]:
    """The engine the runtime will actually start: the pinned image under the container sandbox, the host
    binary otherwise (worker_runtime.executor._prepare)."""
    if sandbox.get("status") == "READY" and sandbox.get("kind") == "container":
        try:
            from aeos.worker_runtime import facade as runtime

            img = ((runtime.sandbox_capabilities().get("levels") or {}).get("container") or {}).get("image") or {}
        except Exception as exc:  # docker CLI missing/unreachable: never a pass
            return [f"worker image UNKNOWN ({type(exc).__name__}); container sandbox cannot start the engine"]
        return [] if img.get("present") else [f"worker image {img.get('tag')} missing: {img.get('evidence')}"]
    from aeos.worker_runtime.facade import opencode_status

    st = opencode_status()
    return [] if st["status"] == "INSTALLED" else [f"OpenCode {st['status']}: {st['evidence']}"]


def prerequisites(session: Session, agent: AgentDefinition, *, domain: str) -> dict[str, Any]:
    """{"ready": bool, "reasons": [...], "sandbox": {...}} - what still blocks enabling *agent* (read-only)."""
    from aeos.worker_runtime.facade import sandbox_readiness
    from aeos.workforce import integrations as ext

    reasons: list[str] = []
    sb: dict[str, Any] = {}
    if agent.harness == "opencode":
        sb = sandbox_readiness(session, domain=domain)
        reasons += _engine_reasons(sb)
        if sb["status"] != "READY":
            reasons += list(sb.get("reasons") or [])
        from aeos.routing import facade as routing

        try:
            rd = routing.eligible_routes(session, domain=domain, data_classification="INTERNAL")
            if not rd.eligible:
                reasons.append("no APPROVED worker route is eligible for this domain")
        except ImportError:
            reasons.append("routing module unavailable; failing closed")
        # Every CAN_RUN_WORKER prerequisite except this agent itself (the gate also requires the agent to be
        # enabled; asking for the full gate here would make enabling impossible).
        ok, cap_reasons = ext.capability_ready(session, Capability.CAN_RUN_WORKER, domain=domain, project_id=None,
                                               exclude_checks=(AGENT_CHECK,))
        if not ok:
            reasons += cap_reasons
    else:
        reasons.append(f"{agent.harness} agents are enabled by their owning module, not here")
    return {"agent_key": agent.agent_key, "harness": agent.harness, "enabled": bool(agent.enabled),
            "ready": not reasons, "reasons": [redact_text(r)[:500] for r in reasons],
            "sandbox": {"status": sb.get("status"), "kind": sb.get("kind")} if sb else None,
            "excluded_check": AGENT_CHECK}


def _recheck(session: Session, *, actor: str) -> dict[str, Any]:
    """Re-run the DB-only agent check so CAN_RUN_WORKER does not keep the result this action made STALE."""
    try:
        from aeos.doctor import facade as doctor

        with session.begin_nested():  # a failed recheck leaves no partial Doctor run behind
            results = doctor.recheck(session, checks=[AGENT_CHECK], actor=actor, trigger="workforce.agent")
        return {"status": "RECHECKED", "results": results}
    except Exception as exc:  # the toggle stands; the stored result goes STALE by its fingerprint
        log.warning("doctor recheck after agent toggle failed: %s", exc)
        return {"status": "NOT_RECHECKED", "reason": f"{type(exc).__name__}: run aeos doctor -c CAN_RUN_WORKER"}


def enable_agent(session: Session, agent_key: str, *, domain: str, actor: str, actor_type: str,
                 reason: str) -> dict[str, Any]:
    from aeos.audit import service as audit
    from aeos.events import service as events

    agent = get_agent(session, agent_key)
    pre = prerequisites(session, agent, domain=domain)
    if not pre["ready"]:
        audit.record(session, actor_type=actor_type, actor_id=actor, action="workforce.agent.enable", result="DENIED",
                     target_type="agent_definition", target_id=agent_key, domain=domain, reason=reason[:500],
                     details={"reasons": pre["reasons"][:20]})
        raise Blocked("agent cannot be enabled yet", code="AGENT_PREREQUISITES", details={"reasons": pre["reasons"]})
    agent.enabled = True
    agent.enabled_reason = redact_text(f"enabled by owner: {reason}")[:500]
    session.flush()
    audit.record(session, actor_type=actor_type, actor_id=actor, action="workforce.agent.enable",
                 target_type="agent_definition", target_id=agent_key, domain=domain, reason=reason[:500])
    events.emit(session, source="controller", kind="scheduler.agent_enabled", entity_type="agent_definition",
                entity_id=agent_key, payload={"enabled": True})
    return {"agent_key": agent_key, "enabled": True, "doctor": _recheck(session, actor=actor),
            "note": "enabled is not in use: runs start only for an executed approved plan, through admission"}


def disable_agent(session: Session, agent_key: str, *, actor: str, actor_type: str, reason: str) -> dict[str, Any]:
    """Always allowed (the safe direction); running attempts are not killed - cancel them explicitly."""
    from aeos.audit import service as audit
    from aeos.events import service as events

    agent = get_agent(session, agent_key)
    agent.enabled = False
    agent.enabled_reason = redact_text(f"disabled by owner: {reason}")[:500]
    session.flush()
    audit.record(session, actor_type=actor_type, actor_id=actor, action="workforce.agent.disable",
                 target_type="agent_definition", target_id=agent_key, reason=reason[:500])
    events.emit(session, source="controller", kind="scheduler.agent_disabled", entity_type="agent_definition",
                entity_id=agent_key, payload={"enabled": False})
    return {"agent_key": agent_key, "enabled": False, "doctor": _recheck(session, actor=actor),
            "note": "no new run is admitted for this agent; running attempts continue until done or cancelled"}
