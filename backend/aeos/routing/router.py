"""Models & Routes API (§23, §35, §37, §43)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.api.deps import DB, AdminPrincipal, CurrentPrincipal, SettingsDep
from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Forbidden, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.events.service import emit
from aeos.identity.service import Principal
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.routing import catalog, service

router = APIRouter(tags=["routing"])


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _visible_domains(principal: Principal) -> list[str | None]:
    """§35/§50: usage from another domain never shows in this view (no Office metadata in Personal)."""
    d = principal.active_domain
    return [None, d.value] if d is not None else [None]


def _last_used(session: Session, route_key: str, principal: Principal) -> dict[str, Any] | None:
    """Latest usage visible in this session's domain (filtered in SQL: a burst of usage from the other
    domain can neither hide the visible one nor leak through)."""
    named = [d for d in _visible_domains(principal) if d is not None]
    scope = or_(UsageLedgerEntry.domain.is_(None), UsageLedgerEntry.domain.in_(named)) if named \
        else UsageLedgerEntry.domain.is_(None)
    q = (select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == route_key, scope)
         .order_by(UsageLedgerEntry.at.desc()).limit(1))
    e = session.scalars(q).first()
    if e is None:
        return None
    return {"at": _iso(e.at), "run_id": e.run_id, "model": e.model, "source": e.source, "mode": e.mode,
            "status": "LAST_USED"}


def _eval_summary(r: ModelRoute) -> dict[str, Any]:
    return service.eval_summary(r)


def _active_decision_view(session: Session, r: ModelRoute) -> dict[str, Any] | None:
    for kind in ("ROUTE_ACTIVATION", "TRIAL"):
        d = service.find_route_decision(session, kind=kind, route=r)
        if d is not None:
            details = d.details or {}
            return {"id": d.id, "kind": d.kind, "expires_at": _iso(d.expires_at), "created_at": _iso(d.created_at),
                    "budget_decision_id": details.get("budget_decision_id"),
                    "eval_run_ids": details.get("eval_run_ids") or [], "rollback_target": details.get("rollback_target"),
                    "terms_review": details.get("terms_review")}
    return None


def _runtime_view(r: ModelRoute, runtime: dict[str, Any]) -> dict[str, Any]:
    fields = service.binding_fields(r)
    drift = service.binding_drift(r, runtime) if r.status in {"APPROVED", "TRIAL"} else []
    current_fields = (*fields, "harness_version") if service.runs_on_worker_runtime(r) else fields
    return {"bound": {f: getattr(r, f) for f in (*fields, "harness_version")},
            "current": {f: runtime.get(f) for f in current_fields},
            "status": "DRIFT" if drift else ("BOUND" if r.status in {"APPROVED", "TRIAL"} else "NOT_ACTIVATED"),
            "drift": drift}


def _execution_path(r: ModelRoute) -> dict[str, Any]:
    if r.pool == "SUPERVISOR":
        return {"status": "NATIVE_BRIDGE", "note": "Hanya lewat binary claude resmi (planning/review)."}
    if r.pool in service.WORKER_EXECUTED_POOLS:
        return {"status": "WORKER_RUNTIME", "note": "Dijalankan worker runtime lewat broker."}
    probe_pools: tuple[str, ...] = ()
    try:
        from aeos.broker import facade as broker_facade

        probe_pools = broker_facade.PROBE_POOLS
    except ImportError:  # broker unavailable: nothing can execute
        pass
    if r.pool in probe_pools:
        return {"status": "FEASIBILITY_PROBE_ONLY",
                "note": f"Pool {r.pool}: tidak melayani task worker. Uji kelayakan lewat probe broker (satu prompt "
                        "sintetis PUBLIC, route gratis berstatus TRIAL/APPROVED oleh pemilik): POST /api/broker/routes/"
                        f"{r.route_key}/feasibility atau aeos broker probe-route (ADR-0111). EvalRun ke pool ini "
                        "belum tersambung."}
    return {"status": "NOT_WIRED",
            "note": f"Pool {r.pool}: broker tidak melayani route ini (bukan task worker, bukan probe kelayakan)."}


def route_view(session: Session, r: ModelRoute, principal: Principal,
               runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    if r.config_hash is None:
        r.config_hash = service.compute_config_hash(r)
    runtime = runtime if runtime is not None else service.runtime_context()
    q = service.quota_exhausted(session, r)
    return {
        "route_key": r.route_key, "pool": r.pool, "provider": r.provider, "endpoint": r.endpoint,
        "model_id": r.model_id, "alias": r.alias, "display_name": r.display_name,
        "resolved_version": r.resolved_version or "UNKNOWN", "resolved_observed_at": _iso(r.resolved_observed_at),
        "identity_status": r.identity_status, "is_free": r.is_free,
        "effectively_free": service.is_effectively_free(r), "free_until": _iso(r.free_until),
        "expires_note": r.expires_note, "pricing": r.pricing, "pricing_checked_at": _iso(r.pricing_checked_at),
        "data_terms": r.data_terms, "terms_checked_at": _iso(r.terms_checked_at),
        "eligible_domains": r.eligible_domains, "allowed_data_classes": r.allowed_data_classes,
        "context_window": r.context_window, "supports_tools": r.supports_tools, "status": r.status, "mode": r.mode,
        "origin": r.origin, "evidence_level": r.evidence_level, "availability": r.availability,
        "availability_checked_at": _iso(r.availability_checked_at), "harness": r.harness,
        "harness_version": r.harness_version, "mode_thinking": service.mode_thinking(r),
        "tool_set": r.tool_set, "fallback_chain": r.fallback_chain,
        "priority": r.priority, "config_hash": r.config_hash, "config_changed_at": _iso(r.config_changed_at),
        "eval_summary": _eval_summary(r), "last_used": _last_used(session, r.route_key, principal),
        "active_decision": _active_decision_view(session, r),
        "quota": ({"state": "EXHAUSTED", "scope": q.scope, "resets_at": _iso(q.resets_at), "source": q.source}
                  if q else {"state": "UNKNOWN_OR_OK", "note": "tidak ada observasi kuota habis"}),
        "source_refs": r.source_refs, "notes": r.notes,
        "region": r.region, "region_restricted": service.region_restricted(r),
        "terms_restriction": (None if r.pool == "SUPERVISOR"
                              else service.terms_restriction(r.data_terms or {}, r.identity_status)),
        # training/trial flags still UNKNOWN: the owner records their own reading at activation (terms_record)
        "terms_unknown": [] if r.pool == "SUPERVISOR" else service.unknown_terms_keys(r.data_terms or {}),
        "reviewed_tool_set": service.reviewed_tool_set(r), "runtime_binding": _runtime_view(r, runtime),
        "execution_path": _execution_path(r),
    }


@router.get("/api/routes")
def list_routes(db: DB, principal: CurrentPrincipal, pool: str | None = None, status: str | None = None,
                domain: str | None = None) -> dict[str, Any]:
    q = select(ModelRoute).order_by(ModelRoute.pool, ModelRoute.priority, ModelRoute.route_key)
    if pool:
        q = q.where(ModelRoute.pool == pool)
    if status:
        q = q.where(ModelRoute.status == status)
    rows = list(db.scalars(q))
    if domain:
        rows = [r for r in rows if domain in (r.eligible_domains or [])]
    runtime = service.runtime_context()
    items = [route_view(db, r, principal, runtime) for r in rows]
    last = catalog.latest_refresh(db)
    checks = [r.pricing_checked_at for r in rows if r.pricing_checked_at is not None]
    return {
        "items": items,
        "source": "model_routes (seed DESIGN dari master §23 + katalog E1 + observasi bridge)",
        "checked_at": _iso(last.created_at) if last else (_iso(max(checks)) if checks else None),
        "generated_at": _iso(utcnow()),  # when this registry view was read (checked_at null = never refreshed)
        "catalog_refresh": ({"at": _iso(last.created_at), "status": last.status, "results": last.results}
                            if last else None),
        # DESIGN as long as any seeded design row is listed: a few catalog-discovered LIVE rows never
        # relabel the registry as live runtime data (§37, §50 'Katalog blueprint ... belum diprobe')
        "mode": "LIVE" if rows and all(r.mode == "LIVE" for r in rows) else "DESIGN",
        "counts": {"total": len(items), "approved": sum(1 for r in rows if r.status in {"APPROVED", "TRIAL"})},
    }


@router.get("/api/routes/eligibility")
def route_eligibility(
    db: DB, principal: CurrentPrincipal, domain: str = Query(..., description="Explicit PERSONAL/OFFICE"),
    classification: str = Query(...), role: str | None = None, pool: str = "WORKER", needs_tools: bool = True,
    min_context: int | None = None, tools: list[str] | None = Query(None),
) -> dict[str, Any]:
    if domain not in {d.value for d in Domain}:
        raise ValidationFailed("domain must be PERSONAL or OFFICE", code="DOMAIN_REQUIRED")
    if not principal.can_access_domain(domain):
        raise Forbidden("domain outside your scope")
    decision = service.eligible_routes(db, domain=domain, data_classification=classification, needs_tools=needs_tools,
                                       min_context=min_context, pool=pool, tools=tools)
    ranked = service.rank(db, decision.eligible, role) if decision.eligible else []
    db.rollback()  # read-only: never persist incidental hash backfills from a GET
    return {
        "domain": domain, "classification": classification, "role": role, "pool": pool,
        "eligible": [{"route_key": r.route_key, "rank": i + 1, "basis": r.basis, "score": r.score,
                      "priority": r.priority} for i, r in enumerate(ranked)],
        "rejected": decision.rejected, "reason": decision.reason,
        "checked_at": utcnow().isoformat(), "source": "routing hard filters (§23)", "mode": "LIVE",
    }


@router.post("/api/routes/refresh-catalog")
def refresh_catalog(db: DB, settings: SettingsDep, principal: AdminPrincipal) -> dict[str, Any]:
    row = catalog.refresh_all(db, actor=principal.user.username, settings=settings)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="routes.catalog_refresh",
                 result="FAILED" if row.status == "FAILED" else "SUCCESS",
                 target_type="model_catalog", target_id="openrouter+opencode",
                 details={"status": row.status, "sources": {k: v.get("status") for k, v in row.results.items()}})
    emit(db, source="routing", kind="routes.catalog_refreshed", entity_type="model_catalog", entity_id=row.id,
         payload={"status": row.status})
    db.commit()
    return {"id": row.id, "status": row.status, "results": row.results, "checked_at": _iso(row.created_at),
            "mode": "LIVE"}


@router.get("/api/routes/{route_key:path}")
def get_route(route_key: str, db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    r = service.get_route(db, route_key)
    view = route_view(db, r, principal)
    view["config"] = service.route_config(r)
    decisions = db.scalars(select(Decision).where(Decision.target_type == service.TARGET_TYPE,
                                                  Decision.target_id == r.route_key)
                           .order_by(Decision.created_at.desc()).limit(20))
    current = service.compute_config_hash(r)
    view["decisions"] = [
        {"id": d.id, "kind": d.kind, "status": d.status, "target_hash": d.target_hash,
         "matches_current_config": d.target_hash == current,
         # only an ACTIVE, human, unexpired decision bound to the CURRENT hash authorises the route
         "effective": service.decision_effective(d, r), "actor_kind": d.actor_kind or "HUMAN",
         "created_at": _iso(d.created_at), "expires_at": _iso(d.expires_at), "rationale": d.rationale}
        for d in decisions
    ]
    view["quota_states"] = [
        {"scope": q.scope, "state": q.state, "source": q.source, "observed_at": _iso(q.observed_at),
         "resets_at": _iso(q.resets_at)}
        for q in db.scalars(select(QuotaState).where(QuotaState.provider == r.provider))
    ]
    db.rollback()
    # checked_at = the route's last catalog/pricing check (null = never checked); generated_at = read time
    return {**view, "source": "model_routes + decisions + quota_states",
            "checked_at": _iso(r.pricing_checked_at), "generated_at": _iso(utcnow())}


class TermsRecordBody(BaseModel):
    may_use_inputs_for_training: bool
    trial_only_no_confidential: bool
    note: str = Field(min_length=10, max_length=500)


class ActivateBody(BaseModel):
    mode: Literal["APPROVED", "TRIAL"]
    rationale: str = Field(min_length=10, max_length=4000)
    expected_config_hash: str = Field(min_length=10, max_length=80)
    allowed_data_classes: list[str] | None = None
    budget_decision_id: str | None = None
    expires_in_days: int | None = None
    terms_reviewed: bool = False
    terms_note: str = Field(default="", max_length=500)
    terms_record: TermsRecordBody | None = None
    region: str | None = Field(default=None, max_length=32)
    tool_set: list[str] | None = Field(default=None, max_length=20)
    eval_run_ids: list[str] | None = Field(default=None, max_length=20)
    # §44 DecisionRecord (options, goal, evidence_ids, cost_quality_tradeoff, invalidate_if): required for APPROVED
    decision_record: dict[str, Any] | None = None


@router.post("/api/routes/{route_key:path}/activate")
def activate(route_key: str, body: ActivateBody, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    route, decision = service.activate_route(
        db, route_key, actor_user_id=principal.user.id, mode=body.mode, rationale=body.rationale,
        expected_config_hash=body.expected_config_hash, allowed_data_classes=body.allowed_data_classes,
        budget_decision_id=body.budget_decision_id, expires_in_days=body.expires_in_days,
        terms_reviewed=body.terms_reviewed, terms_note=body.terms_note,
        terms_record=body.terms_record.model_dump() if body.terms_record else None, region=body.region,
        tool_set=body.tool_set, eval_run_ids=body.eval_run_ids, decision_record=body.decision_record,
    )
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="route.activate",
                 target_type=service.TARGET_TYPE, target_id=route.route_key, domain=Domain.PERSONAL.value,
                 details={"mode": body.mode, "decision_id": decision.id, "config_hash": route.config_hash,
                          "budget_decision_id": body.budget_decision_id, "eval_run_ids": body.eval_run_ids or []})
    emit(db, source="routing", kind="route.activated", entity_type=service.TARGET_TYPE, entity_id=route.route_key,
         entity_version=route.config_hash, payload={"mode": body.mode, "decision_id": decision.id})
    db.commit()
    return {"route": route_view(db, route, principal), "decision_id": decision.id, "decision_kind": decision.kind}


class DisableBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/api/routes/{route_key:path}/disable")
def disable(route_key: str, body: DisableBody, db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    route = service.disable_route(db, route_key, reason=body.reason)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="route.disable",
                 target_type=service.TARGET_TYPE, target_id=route.route_key, reason=body.reason)
    emit(db, source="routing", kind="route.disabled", entity_type=service.TARGET_TYPE, entity_id=route.route_key,
         payload={"reason": body.reason})
    db.commit()
    return {"route": route_view(db, route, principal)}
