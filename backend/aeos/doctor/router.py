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


class BudgetIn(BaseModel):
    cpu_cores: int | None = Field(default=None, ge=1, le=1024)
    ram_gib: float | None = Field(default=None, gt=0, le=65536)
    disk_gib: float | None = Field(default=None, gt=0, le=1_000_000)
    max_parallel_workers: int | None = Field(default=None, ge=1, le=256)
    reason: str = Field(min_length=5, max_length=500)


class ExposureAcceptIn(BaseModel):
    rationale: str = Field(min_length=10, max_length=2000)


def _domain(principal: Any) -> str | None:
    d = principal.active_domain
    return d.value if d is not None else None


def _project(principal: Any) -> str | None:
    pid = getattr(principal.session, "selected_project_id", None)
    if pid and principal.allowed_project_ids is not None and pid not in principal.allowed_project_ids:
        return None
    return pid


@router.get("/api/doctor/checks")
def list_checks(db: DB, principal: CurrentPrincipal, group: str | None = Query(default=None, max_length=32)
                ) -> dict[str, Any]:
    return views.checks_view(db, domain=_domain(principal), group=group,
                             allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/doctor/capabilities")
def capabilities(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    # §08 "Readiness dievaluasi menurut capability dan target": with a selected project its own targets count
    return views.capabilities_view(db, domain=_domain(principal), project_id=_project(principal))


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
    hidden = views.hidden_check_ids(domain)

    def shown(r: Any) -> bool:
        if r.check_id in hidden:
            return False
        d = (r.scope or {}).get("domain")  # project-scoped results carry their project's domain
        return d is None or d == domain

    items = [row_view(r) for r in report.results if shown(r)]
    return {"run": {"id": report.run_id, "started_at": report.started_at, "finished_at": report.finished_at,
                    "functional": report.functional, "groups": report.groups, "capability": report.capability,
                    "counts": views.visible_counts([i["status"] for i in items])},
            "items": items, "skipped": [s for s in report.skipped if s["check_id"] not in hidden],
            "capabilities": views.capabilities_view(db, domain=domain, project_id=_project(principal))["items"]}


@router.get("/api/doctor/resource-budget")
def get_resource_budget(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    from aeos.doctor.checks.host import resource_budget

    b = resource_budget(db)
    return {"budget": b, "status": "SET" if b else "UNKNOWN",
            "note": None if b else "Budget resource belum ditetapkan pemilik dari mesin nyata (§51 P2).",
            "source": "system setting doctor.resource_budget", "mode": "LIVE"}


@router.put("/api/doctor/resource-budget")
def put_resource_budget(body: BudgetIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    """Owner-set hardware/OS resource budget (audited). Host checks compare against it; nothing is measured here."""
    from aeos.doctor.checks.host import set_resource_budget

    value = set_resource_budget(db, actor=principal.user.id, reason=body.reason, cpu_cores=body.cpu_cores,
                                ram_gib=body.ram_gib, disk_gib=body.disk_gib,
                                max_parallel_workers=body.max_parallel_workers)
    db.commit()
    return {"budget": value, "status": "SET"}


@router.post("/api/doctor/secret-exposures/{finding_id}/accept", status_code=201)
def accept_exposure(finding_id: str, body: ExposureAcceptIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    """Human RISK_ACCEPTANCE for one persisted raw-secret finding after the credential was rotated (§19: risk
    acceptance is a human decision bound to the exact target)."""
    from aeos.core.errors import NotFound
    from aeos.core.hashing import content_hash
    from aeos.doctor.checks.secret import EXPOSURE_TARGET, SCAN_STATE_KEY
    from aeos.models.identity import SystemSetting
    from aeos.policy import facade as policy

    row = db.get(SystemSetting, SCAN_STATE_KEY)
    findings = {f.get("id"): f for f in ((row.value or {}).get("findings") or [])} if row is not None else {}
    finding = findings.get(finding_id)
    if finding is None:
        raise NotFound("finding not found")
    d = policy.record_decision(db, kind="RISK_ACCEPTANCE", actor_user_id=principal.user.id,
                               target_type=EXPOSURE_TARGET, target_id=finding_id, target_hash=content_hash(finding),
                               allowed_operations=["doctor.secret_exposure.accept"], rationale=body.rationale,
                               details={"table": finding.get("table"), "found_at": finding.get("found_at")})
    db.commit()
    return {"decision_id": d.id, "finding_id": finding_id, "status": "ACCEPTED"}
