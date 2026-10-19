"""Inventory / System Map API (§35-§38). Read endpoints are domain-scoped projections; refresh, snapshot creation
and approval, component decisions and maintainer edits are SUPER_ADMIN writes. Refresh runs read-only probes only —
it never installs, upgrades, enables or adopts anything (§38 "Refresh/read tidak memperoleh write/install
authority"). Component decisions are recorded through the policy decision registry (human-only AUTH).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep
from aeos.audit import service as audit
from aeos.core.errors import Conflict
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.inventory import snapshots as snaps
from aeos.inventory import views
from aeos.inventory.reconcile import latest_run, reconcile, run_view

router = APIRouter(tags=["inventory"])
LENS_PATTERN = "^(DESIGN|CONFIG|OBSERVED|design|config|observed)$"
# A full (core-set) refresh probes every core component; a repeat within this window is refused with a
# retry hint (rate limit, §38 API). A single-component refresh is bounded by the per-scope concurrency guard.
FULL_REFRESH_MIN_INTERVAL = timedelta(seconds=10)


class RefreshIn(BaseModel):
    component_id: str | None = Field(default=None, max_length=96)


class SnapshotIn(BaseModel):
    label: str = Field(default="", max_length=128)


class ApproveIn(BaseModel):
    rationale: str = Field(min_length=10, max_length=2000)


class ComponentDecisionIn(BaseModel):
    admission: str | None = Field(default=None, max_length=32)
    lifecycle_final: str | None = Field(default=None, max_length=16)
    rationale: str = Field(min_length=10, max_length=2000)


class MaintainerIn(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    reason: str = Field(default="", max_length=300)


def _project(principal: Any, project_id: str | None) -> str | None:
    if project_id is None:
        return None
    if principal.allowed_project_ids is not None and project_id not in principal.allowed_project_ids:
        from aeos.core.errors import NotFound

        raise NotFound("project not found")
    return project_id


@router.get("/api/inventory/components")
def list_components(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
    kind: str | None = None, requirement: str | None = None, category: str | None = None,
    design_label: str | None = None, q: str | None = Query(default=None, max_length=200),
    include_tombstoned: bool = False, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200), view: str = Query(default="default", pattern="^(default|advanced)$"),
    project_id: str | None = Query(default=None, max_length=36),
) -> dict[str, Any]:
    return views.list_components(db, domain=domain.value, kind=kind, requirement=requirement, category=category,
                                 design_label=design_label, q=q, include_tombstoned=include_tombstoned, page=page,
                                 page_size=page_size, view=view, allowed_project_ids=principal.allowed_project_ids,
                                 project_id=_project(principal, project_id))


@router.get("/api/inventory/components/{component_id}")
def component_detail(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return views.component_detail(db, component_id, domain=domain.value,
                                  allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/inventory/components/{component_id}/relations")
def component_relations(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                        page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
    return views.component_relations(db, component_id, domain=domain.value, page=page, page_size=page_size,
                                     allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/inventory/components/{component_id}/dependencies")
def component_dependencies(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                           page: int = Query(default=1, ge=1), page_size: int = Query(default=100, ge=1, le=500),
                           q: str | None = Query(default=None, max_length=200),
                           relation: str | None = Query(default=None, pattern="^(direct|transitive)$")
                           ) -> dict[str, Any]:
    return views.component_dependencies(db, component_id, domain=domain.value, page=page, page_size=page_size, q=q,
                                        relation=relation)


@router.get("/api/inventory/sbom")
def sbom(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> JSONResponse:
    """CycloneDX 1.5 JSON SBOM of the platform's lockfiles (declarations of what the builds link, §37)."""
    from aeos import __version__
    from aeos.core.paths import repo_root
    from aeos.inventory.dependencies import cyclonedx

    body = cyclonedx(repo_root(), version=__version__)
    return JSONResponse(body, headers={"Content-Disposition": 'attachment; filename="dly-aeos.cdx.json"'})


@router.post("/api/inventory/components/{component_id}/decision", status_code=201)
def component_decision(component_id: str, body: ComponentDecisionIn, request: Request, db: DB,
                       principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    """Human decision on one component (e.g. SUSPENDED admission after a security finding, QUARANTINED or RETIRED
    lifecycle), recorded in the Decision registry bound to the component and its manifest hash."""
    from aeos.inventory.decisions import record_component_decision

    views.component_detail(db, component_id, domain=domain.value)  # 404 outside the domain scope
    d = record_component_decision(db, component_id=component_id, actor_user_id=principal.user.id,
                                  rationale=body.rationale, admission=body.admission,
                                  lifecycle_final=body.lifecycle_final)
    db.commit()
    return {"decision_id": d.id, "component": views.component_detail(db, component_id, domain=domain.value)["lifecycle"]}


@router.put("/api/inventory/components/{component_id}/maintainer")
def put_maintainer(component_id: str, body: MaintainerIn, db: DB, principal: AdminPrincipal,
                   domain: ActiveDomain) -> dict[str, Any]:
    from aeos.inventory.decisions import set_maintainer

    views.component_detail(db, component_id, domain=domain.value)  # 404 outside the domain scope
    value = set_maintainer(db, component_id=component_id, name=body.name, actor=principal.user.id, reason=body.reason)
    db.commit()
    return {"component_id": component_id, "maintainer": value}


@router.get("/api/inventory/map")
def system_map(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
               lens: str = Query(default="DESIGN", pattern=LENS_PATTERN),
               category: str | None = None, project_id: str | None = Query(default=None, max_length=36),
               environment: str | None = Query(default=None, max_length=32),
               snapshot: str | None = Query(default=None, max_length=36)) -> dict[str, Any]:
    return views.system_map(db, lens=lens, domain=domain.value, category=category,
                            project_id=_project(principal, project_id), environment=environment, snapshot_id=snapshot,
                            allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/inventory/drift")
def drift(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, include_resolved: bool = False,
          page: int = Query(default=1, ge=1), page_size: int = Query(default=200, ge=1, le=500),
          drift_class: str | None = Query(default=None, max_length=40)) -> dict[str, Any]:
    return views.drift_list(db, domain=domain.value, include_resolved=include_resolved, page=page, page_size=page_size,
                            drift_class=drift_class)


@router.get("/api/inventory/coverage")
def coverage(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return views.coverage(db, domain=domain.value, allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/inventory/blast-radius/{component_id}")
def blast_radius(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                 direction: str = Query(default="dependents", pattern="^(dependents|dependencies)$"),
                 max_depth: int = Query(default=6, ge=1, le=12),
                 lens: str = Query(default="DESIGN", pattern=LENS_PATTERN)) -> dict[str, Any]:
    return views.blast_radius(db, component_id, domain=domain.value, direction=direction, max_depth=max_depth,
                              lens=lens)


@router.get("/api/inventory/path")
def path(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
         source: str = Query(alias="from", max_length=96), target: str = Query(alias="to", max_length=96),
         lens: str = Query(default="DESIGN", pattern=LENS_PATTERN)) -> dict[str, Any]:
    return views.find_path(db, source, target, domain=domain.value, lens=lens)


@router.post("/api/inventory/refresh")
def refresh(body: RefreshIn, request: Request, db: DB, principal: AdminPrincipal, domain: ActiveDomain,
            settings: SettingsDep) -> dict[str, Any]:
    """Scoped refresh of one component or the core set: read-only probes, no install (§38)."""
    if body.component_id is not None:
        views.component_detail(db, body.component_id, domain=domain.value)  # 404 outside the domain scope
    else:
        last = latest_run(db, full_only=True)
        finished = ensure_aware(last.finished_at) if last is not None else None
        if last is not None and finished is not None and utcnow() - finished < FULL_REFRESH_MIN_INTERVAL:
            wait = int((FULL_REFRESH_MIN_INTERVAL - (utcnow() - finished)).total_seconds()) + 1
            raise Conflict("the full inventory was just reconciled; retry shortly", code="REFRESH_TOO_SOON",
                           details={"retry_after_seconds": wait, "last_run_id": last.id})
    run = reconcile(db, settings, component_id=body.component_id, actor=principal.user.id, trigger="api")
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="inventory.refresh",
                 target_type="reconciliation_run", target_id=run.id, domain=domain.value,
                 request_id=getattr(request.state, "request_id", ""),
                 details={"scope": run.scope, "status": run.status, "summary": run.summary})
    db.commit()
    return {"run": run_view(run)}


@router.post("/api/inventory/snapshots")
def create_snapshot(body: SnapshotIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    snap = snaps.create_snapshot(db, label=body.label, domain=domain.value, actor=principal.user.id)
    db.commit()
    return {**snaps.snapshot_meta(snap, db), "refs": snap.refs}


@router.get("/api/inventory/snapshots")
def list_snapshots(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, page: int = Query(default=1, ge=1),
                   page_size: int = Query(default=50, ge=1, le=200), approved_only: bool = False) -> dict[str, Any]:
    out = snaps.list_snapshots_page(db, domain=domain.value, page=page, page_size=page_size,
                                    approved_only=approved_only)
    items = out["items"]
    return {**out, "source": "ArchitectureSnapshot (immutable)",
            "checked_at": items[0]["created_at"] if items else None, "mode": "DESIGN"}


@router.get("/api/inventory/snapshots/diff")
def diff_snapshots(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                   a: str = Query(max_length=36), b: str = Query(max_length=36)) -> dict[str, Any]:
    return snaps.diff_snapshots(db, a, b, domain=domain.value)


@router.get("/api/inventory/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    snap = snaps.get_snapshot(db, snapshot_id, domain=domain.value)
    return {**snaps.snapshot_meta(snap, db), "refs": snap.refs}


@router.post("/api/inventory/snapshots/{snapshot_id}/approve", status_code=201)
def approve_snapshot(snapshot_id: str, body: ApproveIn, db: DB, principal: AdminPrincipal,
                     domain: ActiveDomain) -> dict[str, Any]:
    """Human APPROVAL bound to the snapshot's exact hash; the System Map diff defaults to approved snapshots (§41)."""
    meta = snaps.approve_snapshot(db, snapshot_id, domain=domain.value, actor_user_id=principal.user.id,
                                  rationale=body.rationale)
    db.commit()
    return meta
