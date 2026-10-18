"""Inventory / System Map API (§35-§38). Read endpoints are domain-scoped projections; refresh and
snapshot creation are SUPER_ADMIN writes. Refresh runs read-only probes only — it never installs,
upgrades, enables or adopts anything (§38 "Refresh/read tidak memperoleh write/install authority").
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep
from aeos.audit import service as audit
from aeos.inventory import snapshots as snaps
from aeos.inventory import views
from aeos.inventory.reconcile import reconcile, run_view

router = APIRouter(tags=["inventory"])
LENS_PATTERN = "^(DESIGN|CONFIG|OBSERVED|design|config|observed)$"


class RefreshIn(BaseModel):
    component_id: str | None = Field(default=None, max_length=96)


class SnapshotIn(BaseModel):
    label: str = Field(default="", max_length=128)


@router.get("/api/inventory/components")
def list_components(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
    kind: str | None = None, requirement: str | None = None, category: str | None = None,
    design_label: str | None = None, q: str | None = Query(default=None, max_length=200),
    include_tombstoned: bool = False, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200), view: str = Query(default="default", pattern="^(default|advanced)$"),
) -> dict[str, Any]:
    return views.list_components(db, domain=domain.value, kind=kind, requirement=requirement, category=category,
                                 design_label=design_label, q=q, include_tombstoned=include_tombstoned, page=page,
                                 page_size=page_size, view=view)


@router.get("/api/inventory/components/{component_id}")
def component_detail(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return views.component_detail(db, component_id, domain=domain.value)


@router.get("/api/inventory/components/{component_id}/relations")
def component_relations(component_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return views.component_relations(db, component_id, domain=domain.value)


@router.get("/api/inventory/map")
def system_map(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
               lens: str = Query(default="DESIGN", pattern=LENS_PATTERN),
               category: str | None = None) -> dict[str, Any]:
    return views.system_map(db, lens=lens, domain=domain.value, category=category)


@router.get("/api/inventory/drift")
def drift(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, include_resolved: bool = False) -> dict[str, Any]:
    return views.drift_list(db, domain=domain.value, include_resolved=include_resolved)


@router.get("/api/inventory/coverage")
def coverage(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return views.coverage(db, domain=domain.value)


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
    return {**snaps.snapshot_meta(snap), "refs": snap.refs}


@router.get("/api/inventory/snapshots")
def list_snapshots(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    items = snaps.list_snapshots(db, domain=domain.value)
    return {"items": items, "source": "ArchitectureSnapshot (immutable)",
            "checked_at": items[0]["created_at"] if items else None, "mode": "DESIGN"}


@router.get("/api/inventory/snapshots/diff")
def diff_snapshots(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                   a: str = Query(max_length=36), b: str = Query(max_length=36)) -> dict[str, Any]:
    return snaps.diff_snapshots(db, a, b, domain=domain.value)


@router.get("/api/inventory/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    snap = snaps.get_snapshot(db, snapshot_id, domain=domain.value)
    return {**snaps.snapshot_meta(snap), "refs": snap.refs}
