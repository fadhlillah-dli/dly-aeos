"""Monitoring & Decision Queue (§35): Control Center projections.

Every endpoint is read-only and domain-scoped (ActiveDomain + user project scope enforced in the
backend). Projections never create a second tracker, state or authority (§35 RunView rule).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from aeos.api.deps import DB, ActiveDomain, CurrentPrincipal, SettingsDep, load_project
from aeos.control_center import overview as overview_mod
from aeos.control_center import projections
from aeos.core.errors import NotFound, ValidationFailed
from aeos.models.portfolio import Project

router = APIRouter(tags=["control_center"])


def _project(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, project_id: str | None) -> Project | None:
    if project_id:
        return load_project(db, principal, project_id, domain=domain)
    selected = principal.session.selected_project_id
    if not selected:
        return None
    try:
        return load_project(db, principal, selected, domain=domain)
    except NotFound:  # stale selection: show the domain-level overview instead of leaking/erroring
        return None


@router.get("/api/control-center/overview")
def overview(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, settings: SettingsDep,
             project_id: str | None = None) -> dict[str, Any]:
    project = _project(db, principal, domain, project_id)
    return overview_mod.build_overview(db, settings, principal, domain, project)


@router.get("/api/control-center/activity")
def activity(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, before: int | None = Query(None, ge=1),
             limit: int = Query(50, ge=1, le=200), kind: str | None = Query(None, max_length=96),
             project_id: str | None = None) -> dict[str, Any]:
    if project_id:
        load_project(db, principal, project_id, domain=domain)
    return projections.activity(db, principal, domain, before=before, limit=limit, kind=kind, project_id=project_id)


@router.get("/api/control-center/integrations")
def integrations(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                 project_id: str | None = None) -> dict[str, Any]:
    if project_id:
        load_project(db, principal, project_id, domain=domain)
    return projections.integrations(db, principal, domain, project_id)


@router.get("/api/control-center/search")
def search(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, q: str = Query(..., max_length=100),
           limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    if len(q.strip()) < 2:
        raise ValidationFailed("search query needs at least 2 characters")
    return projections.search(db, principal, domain, q, limit=limit)


@router.get("/api/control-center/history/audit")
def audit_history(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                  action: str | None = Query(None, max_length=128), target_type: str | None = Query(None, max_length=64),
                  target_id: str | None = Query(None, max_length=128),
                  result: str | None = Query(None, pattern="^(SUCCESS|DENIED|FAILED)$"),
                  before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return projections.audit_timeline(db, principal, domain, action=action, target_type=target_type,
                                      target_id=target_id, result=result, before=before, limit=limit)


@router.get("/api/control-center/history/attempts")
def attempt_history(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                    outcome: str | None = Query(None, pattern="^(ACCEPTED|FAILED|CANCELLED)$"),
                    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return projections.attempt_timeline(db, principal, domain, outcome=outcome, page=page, page_size=page_size)


@router.get("/api/control-center/history/decisions")
def decision_history(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                     limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    return projections.decision_timeline(db, principal, domain, limit=limit)


@router.get("/api/control-center/history/snapshots")
def snapshot_history(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                     limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return projections.snapshot_history(db, limit=limit)


@router.get("/api/control-center/history/publications")
def publication_history(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                        limit: int = Query(100, ge=1, le=300)) -> dict[str, Any]:
    return projections.publication_history(db, principal, domain, limit=limit)
