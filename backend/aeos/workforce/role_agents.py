"""Role -> agent resolution (ADR-0126, owner decision R31 of 2026-10-28).

A task runs on the one agent whose ``allowed_scope.roles`` (else its own ``role_key``) lists the task's role, taken
from the reviewed catalog (``catalog.role_agent_map``). A role that no OpenCode worker agent serves is never given
to ``opencode-implementer`` as a default: the task is refused with ``ROLE_UNKNOWN`` and the reason says why. Whether
the mapped agent is seeded and enabled is the admission check's job (``AGENT_DISABLED``, an owner action clears it).

No database access and no LLM: the map is the code catalog, so readiness, enqueue, repair and admission agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from aeos.core.model_policy import model_family_refusal
from aeos.workforce import catalog

ROLE_UNKNOWN = "ROLE_UNKNOWN"


@dataclass(frozen=True)
class RoleAgent:
    role_key: str | None
    agent_key: str | None
    code: str | None = None  # ROLE_UNKNOWN when no OpenCode worker agent serves the role
    reason: str = ""


def _worker_agent(agent_key: str) -> bool:
    spec = next((a for a in catalog.AGENT_DEFINITIONS if a["agent_key"] == agent_key), None)
    return bool(spec and spec.get("harness") == "opencode" and not spec.get("deterministic")
                and agent_key not in catalog.REVIEWER_AGENT_KEYS)


def resolve(role_key: str | None) -> RoleAgent:
    """The worker agent for *role_key*, or ROLE_UNKNOWN with the reason (never a silent implementer default).

    Only OpenCode worker agents count: a non-worker agent with the same role (the deterministic verifier runner of
    ``independent-verifier``, ADR-0060) works in its own trust context and never runs a worker task."""
    agents = catalog.role_agent_map().get(role_key or "", [])
    if not agents:
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"no agent definition serves role {role_key!r}; a role without its own agent is never given "
                         f"to {catalog.IMPLEMENTER_AGENT_KEY} by default (map it in a reviewed catalog change, ADR-0126)")
    workers = [a for a in agents if _worker_agent(a)]
    if len(workers) > 1:
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"role {role_key!r} is served by more than one agent ({', '.join(sorted(workers))}); the "
                         "catalog must name exactly one (ADR-0126)")
    if not workers:
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"role {role_key!r} is served by agent {', '.join(sorted(agents))}, which is not an OpenCode "
                         "worker agent; its tasks are not run by a worker (ADR-0126)")
    return RoleAgent(role_key, workers[0])


def agent_key_for_role(role_key: str | None) -> str | None:
    return resolve(role_key).agent_key


# Executor roles that no OpenCode worker agent serves, with the reason ADR-0126 states for each. Empty: since the
# ADR-0126 amendment of 2026-10-28 independent-verifier tasks run on the coder agent (verification of code and tests
# is Coding work); the deterministic verifier runner keeps its own trusted-suite role (ADR-0060).
UNMAPPED_EXECUTOR_ROLES: dict[str, str] = {}
# Never preferred (ADR-0129, amending ADR-0126): any Claude-family model off the native bridge (AGENTS.md invariant 6:
# Claude only through the native claude binary) and meta-routers (they pick the model themselves, so the ranking would
# not name what runs) - the one check of ``aeos.core.model_policy``. Gemini 4 Argon and the Qwen family are allowed
# since owner answer R34 (2026-10-28: probe them through Go's chat-completions translation); their unproven serving
# keeps them out through eligibility and the probes, never through this list. DeepSeek is allowed (owner R32).


def catalog_problems(definitions: list[dict[str, object]] | None = None,
                     executor_roles: frozenset[str] | None = None) -> list[str]:
    """Findings of the role -> agent map (empty = sound): a role that does not exist, a role two worker agents
    serve, an executor role left without a worker agent and not listed in UNMAPPED_EXECUTOR_ROLES, or a preferred
    route of a model family that is never preferred."""
    defs = catalog.AGENT_DEFINITIONS if definitions is None else definitions
    executors = catalog.EXECUTOR_ROLE_KEYS if executor_roles is None else executor_roles
    known = {str(r["role_key"]) for r in catalog.ALL_ROLES} | set(executors)
    worker = {str(s["agent_key"]) for s in defs if s.get("harness") == "opencode" and not s.get("deterministic")
              and s["agent_key"] not in catalog.REVIEWER_AGENT_KEYS}
    problems: list[str] = []
    served: dict[str, list[str]] = {}
    for spec in defs:
        key = str(spec["agent_key"])
        scope = spec.get("allowed_scope")
        role_key = spec.get("role_key")
        for role in catalog.served_roles(scope if isinstance(scope, dict) else None,
                                         str(role_key) if role_key else None):
            if role not in known:
                problems.append(f"agent {key} serves role {role!r}, which does not exist in the role catalog")
            elif key in worker and role not in executors:
                problems.append(f"worker agent {key} serves role {role!r}, which is not an executor role")
            served.setdefault(role, []).append(key)
        policy = spec.get("route_policy")
        for route in (policy.get("preferred_routes") or []) if isinstance(policy, dict) else []:
            bad = model_family_refusal(str(route))
            if bad:
                problems.append(f"agent {key} prefers route {route} ({bad} is never preferred, AGENTS.md invariant 6, "
                                "ADR-0129)")
    for role, agents in sorted(served.items()):
        workers = [a for a in agents if a in worker]  # a non-worker agent of the same role never runs a worker task
        if len(workers) > 1:
            problems.append(f"role {role!r} is served by more than one agent: {', '.join(sorted(workers))}")
    for role in sorted(executors):
        if not set(served.get(role, [])) & worker and role not in UNMAPPED_EXECUTOR_ROLES:
            problems.append(f"executor role {role!r} has no worker agent and is not listed in UNMAPPED_EXECUTOR_ROLES")
    return problems
