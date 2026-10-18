"""Publisher API (§27-§30, §41): publication preview/execute/reconcile/retry and work-item projection.

Writes require SUPER_ADMIN (VIEWER is refused by the backend) and commit explicitly. Every
project-scoped call runs in the human-selected domain; cross-domain ids are 404 (§35, §50).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep, load_project
from aeos.core.errors import NotFound, PolicyDenied
from aeos.core.timeutil import utcnow
from aeos.models.planning import Plan
from aeos.models.portfolio import Project
from aeos.models.publishing import Publication
from aeos.planning import facade as planning
from aeos.publisher import jobs, work_items
from aeos.publisher import worker as pub_worker
from aeos.publisher.providers import github_issues as gh
from aeos.publisher.providers import google as gg
from aeos.publisher.targets import resolve_targets

router = APIRouter(tags=["publisher"])
SOURCE = "AEOS publication outbox"


class PreviewIn(BaseModel):
    targets: list[Literal["docs", "issues"]] | None = None
    issue_granularity: Literal["task", "feature"] | None = None
    milestone_labels: bool | None = None
    render_visuals: bool = True


class ConfirmIn(BaseModel):
    """The human confirms the exact bound version/hash/preview (stale views are refused)."""

    plan_version_id: str = Field(min_length=1, max_length=64)
    approved_hash: str = Field(min_length=1, max_length=96)
    preview_hash: str = Field(min_length=1, max_length=96)


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


class RetryIn(BaseModel):
    plan_version_id: str | None = None
    approved_hash: str | None = None
    preview_hash: str | None = None


def _load_pub(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, pub_id: str) -> Publication:
    pub = db.get(Publication, pub_id)
    if pub is None:
        raise NotFound("publication not found")
    load_project(db, principal, pub.project_id, domain=domain)  # cross-domain → 404
    return pub


@router.post("/api/plans/{plan_id}/publications/preview")
def preview(plan_id: str, body: PreviewIn, db: DB, settings: SettingsDep, principal: AdminPrincipal,
            domain: ActiveDomain) -> dict[str, Any]:
    plan = planning.get_plan(db, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    load_project(db, principal, plan.project_id, domain=domain)
    opts = body.model_dump(exclude_none=True, exclude={"render_visuals"})
    pub = jobs.create_preview(db, settings, plan_id=plan_id, actor_user_id=principal.user.id, options=opts,
                              render_visuals=body.render_visuals)
    db.commit()
    return jobs.publication_view(db, pub)


@router.get("/api/publications")
def list_publications(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                      project_id: str | None = Query(default=None)) -> dict[str, Any]:
    if project_id:
        ids = [load_project(db, principal, project_id, domain=domain).id]
    else:
        ids = [p.id for p in db.scalars(select(Project).where(Project.domain == domain.value))
               if principal.can_access_project(p.id, p.domain)]
    pubs = jobs.list_publications(db, project_ids=ids, domain=domain.value)
    return {"items": [jobs.publication_view(db, p, with_ops=False) for p in pubs], "source": SOURCE,
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/publications/{pub_id}")
def get_publication(pub_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    pub = _load_pub(db, principal, domain, pub_id)
    view = jobs.publication_view(db, pub)
    view["source"], view["checked_at"] = SOURCE, utcnow().isoformat()
    return view


@router.post("/api/publications/{pub_id}/execute")
def execute(pub_id: str, body: ConfirmIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    _load_pub(db, principal, domain, pub_id)
    outcome = jobs.execute_publication(db, pub_id, actor_user_id=principal.user.id, confirm=body.model_dump())
    db.commit()  # persist the DENIED audit record too
    if not outcome.allowed:
        raise PolicyDenied("external-write interlock denied this publication; nothing was published",
                           code="INTERLOCK_DENIED", details={"reasons": outcome.reasons})
    pub_worker.wake()
    return jobs.publication_view(db, outcome.publication)


@router.post("/api/publications/{pub_id}/reject")
def reject(pub_id: str, body: RejectIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    """§05 step 9: the owner rejects this preview (nothing was or will be published from it)."""
    _load_pub(db, principal, domain, pub_id)
    pub = jobs.reject_preview(db, pub_id, actor_user_id=principal.user.id, reason=body.reason)
    db.commit()
    return jobs.publication_view(db, pub)


@router.post("/api/publications/{pub_id}/reconcile")
def reconcile(pub_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    _load_pub(db, principal, domain, pub_id)
    result = jobs.reconcile_publication(db, pub_id, actor_user_id=principal.user.id)
    db.commit()
    pub = db.get(Publication, pub_id)
    assert pub is not None
    return {"result": result, "publication": jobs.publication_view(db, pub)}


@router.post("/api/publications/{pub_id}/retry")
def retry(pub_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain,
          body: RetryIn | None = None) -> dict[str, Any]:
    _load_pub(db, principal, domain, pub_id)
    outcome = jobs.retry_publication(db, pub_id, actor_user_id=principal.user.id,
                                     confirm=(body.model_dump() if body else None))
    db.commit()
    if not outcome.allowed:
        raise PolicyDenied("external-write interlock denied the retry", code="INTERLOCK_DENIED",
                           details={"reasons": outcome.reasons})
    pub_worker.wake()
    return jobs.publication_view(db, outcome.publication)


@router.get("/api/projects/{project_id}/work-items")
def get_work_items(project_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    return work_items.list_work_items(db, project)


@router.post("/api/projects/{project_id}/work-items/sync")
def sync_work_items(project_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    result = work_items.sync_work_items(db, project, actor_user_id=principal.user.id)
    db.commit()
    return {"result": result, **work_items.list_work_items(db, project)}


@router.get("/api/projects/{project_id}/publication-candidates")
def publication_candidates(project_id: str, db: DB, principal: CurrentPrincipal,
                           domain: ActiveDomain) -> dict[str, Any]:
    """Plans of the project with the version bound by an ACTIVE approval (never "latest")."""
    project = load_project(db, principal, project_id, domain=domain)
    items = []
    for plan in db.scalars(select(Plan).where(Plan.project_id == project.id).order_by(Plan.created_at.desc())):
        entry: dict[str, Any] = {"plan_id": plan.id, "title": plan.title, "state": plan.state,
                                 "current_version_id": plan.current_version_id, "approved": None}
        try:
            v = planning.get_approved_version(db, plan.id)
            entry["approved"] = None if v is None else {"plan_version_id": v.id, "version": v.version,
                                                        "approved_hash": v.manifest_hash,
                                                        "is_latest": v.id == plan.current_version_id}
            entry["approval_status"] = "APPROVED" if v else "NOT_APPROVED"
        except (ImportError, ModuleNotFoundError):
            entry["approval_status"] = "UNKNOWN"
            entry["approval_reason"] = "planning service unavailable — approval cannot be verified"
        items.append(entry)
    return {"items": items, "source": "planning (approved PlanVersion via Decision)", "checked_at":
            utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/projects/{project_id}/publication-targets")
def publication_targets(project_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    targets = resolve_targets(db, project)
    return {"items": [t.to_dict() for t in targets], "source": "integration config + secret status (tanpa nilai)",
            "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "note": "Status READY berarti credential/konfigurasi tersedia (CONFIGURED), belum VERIFIED; panggilan API "
                    "pertama adalah bukti."}


@router.get("/api/publisher/connections")
def connections(principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    out: dict[str, Any] = {"domain": domain.value, "checked_at": utcnow().isoformat(), "mode": "LIVE"}
    if domain.value == "PERSONAL":
        out["google"] = gg.google_status()
        from aeos.core.errors import Blocked
        from aeos.secrets import facade as secrets

        try:
            st = secrets.secret_status(gh.TOKEN_REF)
            out["github"] = {"exists": st.exists, "backend": st.backend, "status": "CONFIGURED" if st.exists
                             else "MISSING", "remedy": gh.CONNECT_REMEDY, "capabilities": gh.CAPABILITIES}
        except Blocked as exc:
            out["github"] = {"status": "BLOCKED", "reason": exc.message, "remedy": gh.CONNECT_REMEDY}
    else:
        out["atlassian"] = {"status": "BLOCKED", "reason": "OFFICE adapters disabled by default; company "
                                                           "authorisation + enabled=true per project integration",
                            "deployment_support": {"CLOUD": "SUPPORTED (mocked tests only; NOT-PROVEN on a tenant)",
                                                   "DATA_CENTER": "NOT_SUPPORTED"}}
    return out
