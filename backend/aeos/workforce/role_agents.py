"""Role -> agent resolution (ADR-0126, owner decision R31 of 2026-10-28).

A task runs on the one agent whose ``allowed_scope.roles`` (else its own ``role_key``) lists the task's role, taken
from the reviewed catalog (``catalog.role_agent_map``). A role that no OpenCode worker agent serves is never given
to ``opencode-implementer`` as a default: the task is refused with ``ROLE_UNKNOWN`` and the reason says why. Whether
the mapped agent is seeded and enabled is the admission check's job (``AGENT_DISABLED``, an owner action clears it).

No database access and no LLM: the map is the code catalog, so readiness, enqueue, repair and admission agree.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """The worker agent for *role_key*, or ROLE_UNKNOWN with the reason (never a silent implementer default)."""
    agents = catalog.role_agent_map().get(role_key or "", [])
    if not agents:
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"no agent definition serves role {role_key!r}; a role without its own agent is never given "
                         f"to {catalog.IMPLEMENTER_AGENT_KEY} by default (map it in a reviewed catalog change, ADR-0126)")
    if len(agents) > 1:
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"role {role_key!r} is served by more than one agent ({', '.join(sorted(agents))}); the "
                         "catalog must name exactly one (ADR-0126)")
    agent_key = agents[0]
    if not _worker_agent(agent_key):
        return RoleAgent(role_key, None, ROLE_UNKNOWN,
                         f"role {role_key!r} is served by agent {agent_key}, which is not an OpenCode worker agent; "
                         "its tasks are not run by a worker (ADR-0126)")
    return RoleAgent(role_key, agent_key)


def agent_key_for_role(role_key: str | None) -> str | None:
    return resolve(role_key).agent_key


# Executor roles that no OpenCode worker agent serves, with the reason ADR-0126 states for each.
UNMAPPED_EXECUTOR_ROLES: dict[str, str] = {
    "independent-verifier": "a deterministic verifier runner (agent independent-verifier, ADR-0060), not an LLM "
                            "worker; a separate trust context from the implementer (§19)",
}
# (marker in the lower-cased route key, family) never preferred (ADR-0126): Gemini 4 Argon and the Qwen family (Go
# serves them on API families the broker does not support; owner answer 2 of 2026-10-28), any Claude-family model
# (AGENTS.md invariant 6: Claude only through the native claude binary) and meta-routers (they pick the model
# themselves, so the ranking would not name what runs). DeepSeek is allowed (owner 2026-10-28, R32: "saya juga ingin
# menggunakan DeepSeek"): its PUBLIC-only data class and the Go region decide through eligibility and probes.
NEVER_PREFERRED: tuple[tuple[str, str], ...] = (
    ("argon", "argon"), ("qwen", "qwen"), ("claude", "claude"), ("anthropic/", "claude"),
    ("openrouter/openrouter/", "meta-router"),
)


def catalog_problems(definitions: list[dict[str, object]] | None = None,
                     executor_roles: frozenset[str] | None = None) -> list[str]:
    """Findings of the role -> agent map (empty = sound): a role that does not exist, a role served twice, an
    executor role left without a worker agent and not listed in UNMAPPED_EXECUTOR_ROLES, or a preferred route of a
    model family that is never preferred."""
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
            bad = next((fam for marker, fam in NEVER_PREFERRED if marker in str(route).lower()), None)
            if bad:
                problems.append(f"agent {key} prefers route {route} ({bad} is never preferred, ADR-0126)")
    for role, agents in sorted(served.items()):
        if len(agents) > 1:
            problems.append(f"role {role!r} is served by more than one agent: {', '.join(sorted(agents))}")
    for role in sorted(executors):
        if not set(served.get(role, [])) & worker and role not in UNMAPPED_EXECUTOR_ROLES:
            problems.append(f"executor role {role!r} has no worker agent and is not listed in UNMAPPED_EXECUTOR_ROLES")
    return problems
