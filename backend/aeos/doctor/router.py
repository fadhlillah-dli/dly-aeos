"""Dashboard Readiness API (§08 layer 2). Reads are available to every signed-in role; running checks
is SUPER_ADMIN only. Remedies are returned as proposals — the API never executes them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aeos.api.deps import DB, AdminPrincipal, CurrentPrincipal, SettingsDep
from aeos.audit import service as audit
from aeos.core.enums import Capability
from aeos.doctor import views
from aeos.doctor.runner import row_view, run_doctor

router = APIRouter(tags=["doctor"])


class RunIn(BaseModel):
    group: str | list[str] | None = Field(default=None)
    capability: Capability | None = None
    functional: bool = False


def _domain(principal: Any) -> str | None:
    d = principal.active_domain
    return d.value if d is not None else None


@router.get("/api/doctor/checks")
def list_checks(db: DB, principal: CurrentPrincipal, group: str | None = Query(default=None, max_length=32)
                ) -> dict[str, Any]:
    return views.checks_view(db, domain=_domain(principal), group=group)


@router.get("/api/doctor/capabilities")
def capabilities(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    return views.capabilities_view(db, domain=_domain(principal))


@router.get("/api/doctor/checks/{check_id}/history")
def check_history(check_id: str, db: DB, principal: CurrentPrincipal,
                  limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    return views.history_view(db, check_id, domain=_domain(principal), limit=limit)


@router.post("/api/doctor/run")
def run(body: RunIn, request: Request, db: DB, principal: AdminPrincipal, settings: SettingsDep) -> dict[str, Any]:
    groups = [body.group] if isinstance(body.group, str) else (body.group or None)
    report = run_doctor(settings, db, groups=groups, capability=body.capability.value if body.capability else None,
                        functional=body.functional, actor=principal.user.id, trigger="api")
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="doctor.run", target_type="doctor_run",
                 target_id=report.run_id or "", request_id=getattr(request.state, "request_id", ""),
                 details={"groups": report.groups, "capability": report.capability, "functional": report.functional,
                          "counts": report.counts(), "skipped": len(report.skipped)})
    db.commit()
    domain = _domain(principal)
    items = [row_view(r) for r in report.results if (r.scope or {}).get("domain") in (None, domain)]
    return {"run": {"id": report.run_id, "started_at": report.started_at, "finished_at": report.finished_at,
                    "functional": report.functional, "groups": report.groups, "capability": report.capability,
                    "counts": report.counts()},
            "items": items, "skipped": report.skipped,
            "capabilities": views.capabilities_view(db, domain=domain)["items"]}
