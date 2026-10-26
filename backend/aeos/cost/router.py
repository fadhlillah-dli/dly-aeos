"""Costs & Quotas API (§35, §42). Every number comes from the ledger/provider/code; UNKNOWN stays
UNKNOWN. Budget changes are owner decisions (AdminPrincipal + recorded BUDGET Decision)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import NotFound, PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.cost import service
from aeos.cost.ccusage import ccusage_estimate
from aeos.identity.facade import Principal
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun

router = APIRouter(tags=["cost"])

SOURCE = "usage_ledger (broker/provider/collector), budgets + BUDGET decisions, quota_states"


def _cross_domain(principal: Principal) -> bool:
    """GLOBAL/ROUTE budget spend aggregates every domain and project, so it is shown only to a
    principal who may see all of them (no metadata/counts outside the scope, §35/§50)."""
    return principal.allowed_project_ids is None and all(principal.can_access_domain(d) for d in Domain)


@router.get("/api/costs/summary")
def costs_summary(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                  period: str = Query("MONTH", pattern="^(DAY|WEEK|MONTH|TOTAL)$")) -> dict[str, Any]:
    return service.summary(db, domain=domain.value, allowed_project_ids=principal.allowed_project_ids,
                           cross_domain_visible=_cross_domain(principal), period=period)


@router.get("/api/costs/ledger")
def costs_ledger(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
    category: str | None = None, project_id: str | None = None, limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    if project_id:
        load_project(db, principal, project_id, domain=domain)
    items = service.list_ledger(db, domain=domain.value, allowed_project_ids=principal.allowed_project_ids,
                                category=category, project_id=project_id, limit=limit)
    return {"items": items, "source": SOURCE, "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/budgets")
def list_budgets(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                 include_superseded: bool = False) -> dict[str, Any]:
    items = service.list_budgets(db, domain=domain.value, allowed_project_ids=principal.allowed_project_ids,
                                 cross_domain_visible=_cross_domain(principal),
                                 include_superseded=include_superseded)
    return {"items": items, "source": "budgets + BUDGET decisions", "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "policy": {"default": "GLOBAL $0 hard stop", "paid_fallback": False, "auto_topup": False,
                       "key_rotation_to_bypass_limits": False}}


class BudgetIn(BaseModel):
    scope_type: str = Field(pattern="^(GLOBAL|PROJECT|ROUTE|RUN)$")
    scope_id: str = "*"
    period: str = Field("MONTH", pattern="^(DAY|WEEK|MONTH|TOTAL)$")
    limit_usd: float | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=0)
    limit_requests: int | None = Field(default=None, ge=0)
    hard_stop: bool = True
    rationale: str = Field(min_length=3, max_length=4000)


@router.post("/api/budgets")
def set_budget(body: BudgetIn, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    project_id: str | None = None
    domain: str | None = None
    if body.scope_type == "PROJECT":
        active = principal.active_domain
        if active is None:
            raise ValidationFailed("select a domain before setting a PROJECT budget")
        project = load_project(db, principal, body.scope_id, domain=active)
        project_id, domain = project.id, project.domain
    elif body.scope_type == "RUN":
        run = db.get(WorkerRun, body.scope_id)
        active = principal.active_domain
        if run is None or active is None or run.domain != active.value:
            raise NotFound("run not found")
        project_id, domain = run.project_id, run.domain
    elif body.scope_type == "ROUTE":
        if db.scalar(select(ModelRoute).where(ModelRoute.route_key == body.scope_id)) is None:
            raise NotFound("route not registered")
    if body.scope_type in ("GLOBAL", "ROUTE") and not _cross_domain(principal):
        # A GLOBAL/ROUTE cap governs spend in every domain and project; a SUPER_ADMIN whose user scope
        # is limited is not AUTH over the others (§20 "SUPER_ADMIN bukan otomatis AUTH").
        raise PolicyDenied("GLOBAL/ROUTE budgets span every domain and project; your user scope is limited",
                           code="BUDGET_SCOPE_DENIED")
    b = service.set_budget(
        db, actor_user_id=principal.user.id, scope_type=body.scope_type, scope_id=body.scope_id,
        period=body.period, limit_usd=body.limit_usd, limit_tokens=body.limit_tokens,
        limit_requests=body.limit_requests, hard_stop=body.hard_stop, rationale=body.rationale,
        project_id=project_id, domain=domain,
    )
    view = service.budget_view(db, b, spend_visible=body.scope_type in ("PROJECT", "RUN") or _cross_domain(principal))
    db.commit()
    return view


@router.get("/api/quotas")
def list_quotas(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": service.list_quotas(db), "source": "quota_states (official / provider-header / observed-429)",
            "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "note": "Sisa kuota langganan Max hanya ditampilkan bila sumber resmi tersedia; selain itu UNKNOWN."}


@router.post("/api/costs/native-estimate")
def native_estimate(db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    """Optional local ccusage estimator (ESTIMATE only, never invoice or quota authority)."""
    if not _cross_domain(principal):
        # ccusage aggregates every local Claude Code session (any domain/project): counts from other
        # domains must not reach a scope-limited principal (§35/§50 "termasuk counts").
        raise PolicyDenied("the ccusage estimate aggregates all local sessions across domains; your scope is limited",
                           code="ESTIMATOR_SCOPE_DENIED")
    result = ccusage_estimate()
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="cost.native_estimate.run",
                 result="SUCCESS" if result.get("status") == "ESTIMATE" else "FAILED",
                 target_type="estimator", target_id="ccusage", details={"status": result.get("status")})
    db.commit()
    return result
