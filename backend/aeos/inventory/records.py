"""Inventory entries that are other modules' records, not manifest components (§36, §37).

Tools & Repos merges the Integration Registry, ModelRoute, RoleSkillPack/AgentDefinition and
repository references that already exist (§37 "bukan sumber authority kedua"). This module only
projects them — it never writes, never creates a ComponentReference for them and never guesses:

* **project repositories** — ``RepositoryReference`` of the projects the session may see (exact
  provider repo id, branch/head/dirty, last verification). Local paths are never returned.
* **project services** — what a managed project ships (§37 "Layanan aplikasi hasil build"): entries
  of the *approved* PlanVersion architecture (bound to an ACTIVE human APPROVAL on its hash) and the
  deployment/framework markers of the read-only repository inventory. They are DECLARED (design),
  never "running", until something observes them.
* **routes / roles** — APPROVED/TRIAL ModelRoutes eligible for the domain and the roles of enabled
  AgentDefinitions, as System Map nodes with typed edges to their provider/harness components.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.ids import slugify
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware
from aeos.inventory.configlens import HARNESS_COMPONENTS, PROVIDER_COMPONENTS, route_eligible

REPO_PROVIDER_COMPONENTS = {"GITHUB": "github", "BITBUCKET": "bitbucket"}
PLAN_TARGET = "plan_version"
MAX_PLAN_ENTRIES = 40


def _iso(dt: Any) -> str | None:
    dt = ensure_aware(dt)
    return dt.isoformat() if dt else None


def visible_projects(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None = None,
                     project_id: str | None = None) -> list[Any]:
    """Projects of the session's domain (never another domain) inside the user's project scope."""
    from aeos.models.portfolio import Project

    if domain is None:
        return []
    q = select(Project).where(Project.domain == domain)
    if project_id is not None:
        q = q.where(Project.id == project_id)
    rows = list(session.scalars(q.order_by(Project.name)))
    if allowed_project_ids is not None:
        rows = [p for p in rows if p.id in allowed_project_ids]
    return rows


def project_repositories(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None = None,
                         project_id: str | None = None) -> list[dict[str, Any]]:
    from aeos.models.portfolio import RepositoryReference

    projects = {p.id: p for p in visible_projects(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                                  project_id=project_id)}
    if not projects:
        return []
    out = []
    for r in session.scalars(select(RepositoryReference).where(RepositoryReference.project_id.in_(list(projects)))
                             .order_by(RepositoryReference.full_name)):
        p = projects[r.project_id]
        provider = (r.provider or "").upper()
        out.append({
            "id": f"repo:{r.id}", "repository_id": r.id, "kind": "project_repository", "project_id": p.id,
            "project_name": p.name, "domain": p.domain, "provider": provider, "full_name": r.full_name,
            "provider_repo_id": r.provider_repo_id,
            # §37: exact repo ID, never "same name = same target"
            "identity": f"{provider.lower()}:{r.provider_repo_id}" if r.provider_repo_id else None,
            "identity_status": "VERIFIED" if r.verification_status == "VERIFIED" and r.provider_repo_id else "UNVERIFIED",
            "role": r.role, "default_branch": r.default_branch, "head_commit": r.head_commit, "dirty": r.dirty,
            "local_branch": r.local_branch, "local_status": r.local_status,
            "verification_status": r.verification_status, "verification_reason": r.verification_reason,
            "last_verified_at": _iso(r.last_verified_at), "url": r.url or None,
            "provider_component": REPO_PROVIDER_COMPONENTS.get(provider),
            "ownership": {"project_owner_user_id": p.owner_user_id, "project_lifecycle": p.lifecycle},
            "link": f"/projects/{p.id}",
        })
    return out


def _approved_versions(session: Session, project_ids: list[str]) -> dict[str, Any]:
    """project_id -> the PlanVersion an ACTIVE human APPROVAL is bound to (exact hash)."""
    from aeos.models.decisions import Decision
    from aeos.models.planning import PlanVersion

    out: dict[str, Any] = {}
    if not project_ids:
        return out
    for d in session.scalars(select(Decision).where(Decision.kind == "APPROVAL", Decision.target_type == PLAN_TARGET,
                                                    Decision.status == "ACTIVE",
                                                    Decision.project_id.in_(project_ids))
                             .order_by(Decision.created_at)):
        v = session.get(PlanVersion, d.target_id)
        if v is None or v.manifest_hash != d.target_hash or d.actor_kind not in ("HUMAN", "HUMAN_RECORDED"):
            continue
        out[v.project_id] = (v, d)
    return out


def _plan_entries(content: dict[str, Any]) -> list[tuple[str, str]]:
    arch = ((content or {}).get("architecture") or {}).get("overview") or {}
    home = (content or {}).get("product_home") or {}
    entries: list[tuple[str, str]] = []
    for item in arch.get("component_architecture") or []:
        entries.append(("architecture.overview.component_architecture", str(item)))
    for item in arch.get("external_integrations") or []:
        entries.append(("architecture.overview.external_integrations", str(item)))
    if home.get("service"):
        entries.append(("product_home.service", str(home["service"])))
    return [(src, redact_text(text).strip()[:200]) for src, text in entries if text and text.strip()][
        :MAX_PLAN_ENTRIES]


def project_services(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None = None,
                     project_id: str | None = None) -> list[dict[str, Any]]:
    from aeos.models.portfolio import RepositoryReference

    projects = {p.id: p for p in visible_projects(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                                  project_id=project_id)}
    if not projects:
        return []
    out: list[dict[str, Any]] = []
    for pid, (v, d) in _approved_versions(session, list(projects)).items():
        p = projects[pid]
        for src, text in _plan_entries(v.content):
            out.append({
                "id": f"svc:{v.id[:8]}:{slugify(text, 40)}", "kind": "project_service", "name": text,
                "project_id": pid, "project_name": p.name, "domain": p.domain, "status": "DECLARED",
                "observed": "UNKNOWN", "observed_reason": "belum ada observasi runtime layanan ini",
                "provenance": f"plan_version:{v.manifest_hash}", "source_field": src, "decision_id": d.id,
                "plan_version_id": v.id, "plan_version": v.version,
                "owner": {"project_owner_user_id": p.owner_user_id}, "lifecycle": p.lifecycle,
            })
    for r in session.scalars(select(RepositoryReference).where(RepositoryReference.project_id.in_(list(projects)))):
        inv = r.inventory or {}
        p = projects[r.project_id]
        markers: list[tuple[str, str]] = []
        for kind, paths in (inv.get("deployment") or {}).items():
            markers.append((f"deployment:{kind}", ", ".join(str(x) for x in (paths or [])[:3]) if isinstance(paths, list)
                            else str(paths)))
        for fw in ((inv.get("tests") or {}).get("frameworks") or [])[:10]:
            markers.append((f"test-framework:{fw}", ""))
        for kind, detail in markers:
            out.append({
                "id": f"svc:{r.id[:8]}:{slugify(kind, 40)}", "kind": "project_service", "name": kind,
                "detail": redact_text(detail)[:200] or None, "project_id": p.id, "project_name": p.name,
                "domain": p.domain, "status": "DECLARED", "observed": "UNKNOWN",
                "observed_reason": "penanda di repository bukan bukti layanan berjalan",
                "provenance": f"repo_inventory:{r.full_name}@{(r.inventory_commit or '')[:12]}",
                "source_field": "repository inventory", "decision_id": None, "plan_version_id": None,
                "owner": {"project_owner_user_id": p.owner_user_id}, "lifecycle": p.lifecycle,
            })
    return out


def route_nodes(session: Session, *, domain: str | None) -> list[dict[str, Any]]:
    """APPROVED/TRIAL ModelRoutes usable in *domain* (OFFICE stays empty until company-approved)."""
    from aeos.models.routing import ModelRoute

    out = []
    for r in session.scalars(select(ModelRoute).where(ModelRoute.status.in_(("APPROVED", "TRIAL")))
                             .order_by(ModelRoute.route_key)):
        if not route_eligible(r, domain):
            continue  # same rule as routing admission; an OFFICE route needs an explicit eligibility (§23)
        out.append({"id": f"route:{r.route_key}", "kind": "route", "route_key": r.route_key, "status": r.status,
                    "pool": r.pool, "provider": r.provider, "model_id": r.model_id,
                    "provider_component": PROVIDER_COMPONENTS.get(r.provider), "config_hash": r.config_hash,
                    "is_free": r.is_free, "free_until": _iso(r.free_until)})
    return out


def role_nodes(session: Session) -> list[dict[str, Any]]:
    """Roles of enabled AgentDefinitions (a role/agent definition, never a fictional running instance)."""
    from aeos.models.workforce import AgentDefinition, RoleSkillPack

    roles = {r.role_key: r for r in session.scalars(select(RoleSkillPack))}
    out: dict[str, dict[str, Any]] = {}
    for a in session.scalars(select(AgentDefinition).where(AgentDefinition.enabled.is_(True))
                             .order_by(AgentDefinition.agent_key)):
        role = roles.get(a.role_key)
        node = out.setdefault(a.role_key, {
            "id": f"role:{a.role_key}", "kind": "role", "role_key": a.role_key,
            "name": role.name if role else a.role_key, "group": role.group if role else None,
            "revision_hash": role.revision_hash if role else None, "agents": [], "harness_components": []})
        node["agents"].append({"agent_key": a.agent_key, "harness": a.harness, "revision_hash": a.revision_hash})
        harness = HARNESS_COMPONENTS.get(a.harness)
        if harness and harness not in node["harness_components"]:
            node["harness_components"].append(harness)
    return list(out.values())
