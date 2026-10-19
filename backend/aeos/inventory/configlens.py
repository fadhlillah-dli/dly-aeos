"""CONFIG lens ("Konfigurasi berlaku", §36): what is enabled and allowed right now, and why.

A component is in the CONFIG lens only with an explicit basis:

1. ``enablement == ENABLED`` on its ComponentReference (set by a human decision flow, never by seed);
2. an APPROVED/TRIAL ModelRoute whose provider maps to it;
3. a ProjectIntegration (in the viewer's domain) whose provider maps to it;
4. an enabled AgentDefinition whose harness maps to it;
5. it is part of the running released controller: a platform-scoped service/module/datastore/
   runtime/manifest/repository whose code or file was observed INSTALLED by a fresh probe.

A default setting value (e.g. the default OpenBao address) is NOT a basis: configured is not enabled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.inventory.observations import is_stale
from aeos.models.inventory import ComponentObservation, ComponentReference

PROVIDER_COMPONENTS = {
    "claude-native": "claude-code", "openrouter": "openrouter", "opencode-zen": "opencode-console",
    "opencode": "opencode-console", "gemini": "gemini-api", "deepseek": "deepseek-api", "litellm": "litellm",
}
INTEGRATION_COMPONENTS = {
    "github": "github", "github-repo": "github", "github-issues": "github-issues", "github-actions": "github-actions",
    "google-drive": "google-drive", "google-docs": "google-docs", "bitbucket": "bitbucket", "jira": "jira",
    "confluence": "confluence",
}
HARNESS_COMPONENTS = {"opencode": "opencode", "claude-native": "claude-native-bridge",
                      "verifier-runner": "verification-review"}
RELEASED_KINDS = frozenset({"service", "platform_module", "datastore", "runtime", "manifest", "repository"})
EXPLICIT_PREFIXES = ("enablement", "model route", "integrasi", "agent definition")


def route_eligible(route: Any, domain: str | None) -> bool:
    """Same rule as routing admission (``aeos.routing.service``): a route serves a domain only when that domain
    is listed in ``eligible_domains``; an empty list serves no domain (fail closed, §23 OFFICE default deny).
    ``domain=None`` is the platform-wide view (every route)."""
    return domain is None or domain in (route.eligible_domains or [])


def config_basis(
    session: Session, comps: dict[str, ComponentReference], latest: dict[tuple[str, str], ComponentObservation],
    now: datetime, *, domain: str | None = None,
) -> dict[str, list[str]]:
    """component_id -> reasons it is in the CONFIG lens (empty/missing = not enabled)."""
    from aeos.models.portfolio import ProjectIntegration
    from aeos.models.routing import ModelRoute
    from aeos.models.workforce import AgentDefinition

    basis: dict[str, list[str]] = {}

    def add(cid: str | None, why: str) -> None:
        if cid and cid in comps and comps[cid].tombstoned_at is None:
            basis.setdefault(cid, []).append(why)

    for cid, c in comps.items():
        if c.enablement == "ENABLED":
            add(cid, "enablement ENABLED (keputusan)")
        if c.kind in RELEASED_KINDS and "PLATFORM" in ((c.scope or {}).get("domains") or ["PLATFORM"]):
            inst = latest.get((cid, "installation"))
            if inst is not None and inst.status == "INSTALLED" and not is_stale(inst, now):
                add(cid, "bagian dari released controller yang berjalan (terpasang, teramati)")
    for r in session.scalars(select(ModelRoute).where(ModelRoute.status.in_(("APPROVED", "TRIAL")))):
        if route_eligible(r, domain):
            add(PROVIDER_COMPONENTS.get(r.provider), f"model route {r.route_key} ({r.status})")
    q = select(ProjectIntegration)
    if domain is not None:
        q = q.where(ProjectIntegration.domain == domain)
    for pi in session.scalars(q):
        add(INTEGRATION_COMPONENTS.get(pi.provider), f"integrasi proyek {pi.provider}/{pi.purpose}")
    for a in session.scalars(select(AgentDefinition).where(AgentDefinition.enabled.is_(True))):
        add(HARNESS_COMPONENTS.get(a.harness), f"agent definition {a.agent_key} enabled")
    return basis


def is_explicit(reasons: list[str] | None) -> bool:
    return any(r.startswith(EXPLICIT_PREFIXES) for r in reasons or [])


def summarize(reasons: list[str] | None) -> dict[str, Any]:
    return {"present": bool(reasons), "basis": list(reasons or []), "explicit": is_explicit(reasons)}
