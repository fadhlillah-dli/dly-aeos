"""Worker model routing (§23, §42, §43) — hard eligibility first, then evaluated selection.

Stage 1 (hard filters, each rejection carries a reason):
  status APPROVED with an ACTIVE human ROUTE_ACTIVATION decision bound to the current config hash,
  or TRIAL with an ACTIVE TRIAL decision bound to it; domain eligible (OFFICE additionally needs
  the office policy: external_worker_allowed + route listed in approved_worker_routes; policy
  unavailable -> deny); data classification allowed; identity UNVERIFIED -> PUBLIC data only;
  tool support proven when tools are needed; context window known and large enough; catalog
  availability not MISSING; quota not EXHAUSTED; pricing/terms freshness; paid (or free status
  unknown/expired) -> also an ACTIVE BUDGET decision referenced by the activation.
Stage 2 (selection): recorded eval results for the task role whose config hash matches the
  current configuration (a changed configuration never inherits an old baseline), otherwise the
  deterministic priority. The model never selects itself; TaskSpec route policy can only narrow.
Fallback: only along the failed route's ``fallback_chain``, only to routes that are themselves
  eligible, same pool, free — never a paid fallback and never a supervisor downgrade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataClassification, Domain
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.cost import QuotaState
from aeos.models.decisions import Decision
from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute
from aeos.routing.facade import RouteDecision

log = logging.getLogger("aeos.routing")

TARGET_TYPE = "model_route"
WORKER_POOLS = ("WORKER", "COMPARATOR", "ARENA", "LOCAL", "WATCHLIST")
ALL_POOLS = ("SUPERVISOR", *WORKER_POOLS)
TERMS_MAX_AGE = timedelta(days=30)
TRIAL_DEFAULT_DAYS = 14
TRIAL_MAX_DAYS = 90
APPROVAL_MAX_DAYS = 365
RESTRICTED_TERMS_KEYS = ("may_use_inputs_for_training", "trial_only_no_confidential")

# Fields that make up the AIConfiguration identity (§43). Status/timestamps/eval are excluded so
# that refreshing a catalog without any change keeps the identity (and approvals) stable.
CONFIG_FIELDS = (
    "route_key", "pool", "provider", "endpoint", "model_id", "alias", "resolved_version", "mode_params", "harness",
    "harness_version", "prompt_revision", "tool_set", "policy_version", "context_strategy", "fallback_chain",
    "is_free", "pricing", "data_terms", "region", "eligible_domains", "allowed_data_classes", "context_window",
    "supports_tools", "identity_status", "free_until",
)


# ------------------------------------------------------------------------------ identity


_LIST_FIELDS = frozenset({"tool_set", "fallback_chain", "eligible_domains", "allowed_data_classes"})
_DICT_FIELDS = frozenset({"mode_params", "pricing", "data_terms"})
_SCALAR_DEFAULTS = {"supports_tools": "UNKNOWN", "is_free": "UNKNOWN", "identity_status": "PROVIDER_CLAIMED"}


def route_config(route: ModelRoute) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for f in CONFIG_FIELDS:
        v = getattr(route, f)
        # column defaults are applied only at flush: None and empty containers are the same identity
        if v is None:
            if f in _LIST_FIELDS:
                v = []
            elif f in _DICT_FIELDS:
                v = {}
            else:
                v = _SCALAR_DEFAULTS.get(f)
        if f == "data_terms" and isinstance(v, dict):
            v = {k: vv for k, vv in v.items() if not k.endswith("_at") and k not in {"catalog", "checked_by"}}
        if isinstance(v, datetime):
            v = ensure_aware(v).date().isoformat()  # type: ignore[union-attr]
        cfg[f] = v
    return cfg


def compute_config_hash(route: ModelRoute) -> str:
    return content_hash(route_config(route))


def refresh_config_hash(session: Session, route: ModelRoute, *, reason: str) -> bool:
    """Recompute the identity. Returns True when a previously known identity changed: approvals
    bound to the old hash stop matching, eval baselines are not inherited, Doctor checks go STALE."""
    new = compute_config_hash(route)
    old = route.config_hash
    if old == new:
        return False
    route.config_hash = new
    if old is None:
        return False
    route.config_changed_at = utcnow()
    try:
        from aeos.policy.facade import invalidate_decisions

        invalidate_decisions(session, target_type=TARGET_TYPE, target_id=route.route_key,
                             reason=f"route configuration changed: {reason}")
    except (ImportError, ModuleNotFoundError):
        pass  # hash binding already makes old approvals ineffective
    try:
        from aeos.doctor.facade import invalidate

        invalidate(session, invalidator="config:routes", reason=f"{route.route_key}: {reason}")
    except (ImportError, ModuleNotFoundError):
        pass
    try:
        from aeos.events.service import emit

        emit(session, source="routing", kind="route.config_changed", entity_type=TARGET_TYPE,
             entity_id=route.route_key, entity_version=new, payload={"reason": reason, "previous_hash": old})
    except Exception:  # pragma: no cover
        log.exception("emit route.config_changed failed")
    return True


def get_route(session: Session, route_key: str) -> ModelRoute:
    route = session.scalars(select(ModelRoute).where(ModelRoute.route_key == route_key)).first()
    if route is None:
        raise NotFound(f"route {route_key} not found")
    return route


# ------------------------------------------------------------------------------ decisions


def _decision_valid(d: Decision | None, *, kind: str, config_hash: str | None) -> bool:
    if d is None or d.kind != kind or d.status != "ACTIVE" or (d.actor_kind or "HUMAN") != "HUMAN":
        return False
    if d.expires_at is not None and ensure_aware(d.expires_at) <= utcnow():  # type: ignore[operator]
        return False
    return config_hash is None or d.target_hash == config_hash


def decision_effective(d: Decision, route: ModelRoute) -> bool:
    """Whether *d* currently authorises *route*: ACTIVE, human, unexpired and bound to the hash of the
    route's CURRENT configuration (what the dashboard may show as approved — nothing else)."""
    if d.kind not in {"ROUTE_ACTIVATION", "TRIAL"}:
        return False
    return _decision_valid(d, kind=d.kind, config_hash=compute_config_hash(route))


def find_route_decision(session: Session, *, kind: str, route: ModelRoute) -> Decision | None:
    """ACTIVE human decision of *kind* bound to the route's CURRENT configuration, else None.

    The hash is recomputed from the live record, so a configuration edited without going through
    ``refresh_config_hash`` can never keep an older approval effective."""
    current = compute_config_hash(route)
    d: Decision | None = None
    try:
        from aeos.policy.facade import find_active_decision

        d = find_active_decision(session, kind=kind, target_type=TARGET_TYPE, target_id=route.route_key,
                                 target_hash=current)
    except (ImportError, ModuleNotFoundError):
        # Decision registry implementation not installed: strict read-only lookup of the canonical
        # table (still human-only, ACTIVE, unexpired, exact hash). Creation always goes via policy.
        rows = session.scalars(
            select(Decision).where(Decision.kind == kind, Decision.target_type == TARGET_TYPE,
                                   Decision.target_id == route.route_key, Decision.status == "ACTIVE")
            .order_by(Decision.created_at.desc())
        )
        d = next((r for r in rows if _decision_valid(r, kind=kind, config_hash=current)), None)
    return d if _decision_valid(d, kind=kind, config_hash=current) else None


def budget_decision_valid(session: Session, decision_id: str | None, route: ModelRoute) -> tuple[bool, str]:
    if not decision_id:
        return False, "paid route requires an explicit BUDGET decision id"
    d = session.get(Decision, decision_id)
    if not _decision_valid(d, kind="BUDGET", config_hash=None):
        return False, "budget decision is missing, not ACTIVE, not human or expired"
    assert d is not None
    routes = (d.auth_scope or {}).get("routes") or []
    covers = (d.target_type == TARGET_TYPE and d.target_id == route.route_key) or route.route_key in routes
    if not covers:
        return False, "budget decision does not cover this route"
    return True, ""


# ------------------------------------------------------------------------------ helpers


def is_effectively_free(route: ModelRoute, now: datetime | None = None) -> bool:
    if route.is_free != "YES":
        return False
    until = ensure_aware(route.free_until)
    return until is None or until > (now or utcnow())


def _office_policy(session: Session) -> tuple[dict[str, Any] | None, str]:
    try:
        from aeos.policy.facade import office_policy

        return office_policy(session), ""
    except (ImportError, ModuleNotFoundError):
        return None, "office policy unavailable (aeos.policy not installed) - OFFICE denied"
    except Exception as exc:  # pragma: no cover - fail closed
        return None, f"office policy could not be evaluated ({type(exc).__name__}) - OFFICE denied"


def _quota_rows(session: Session, route: ModelRoute) -> list[QuotaState]:
    scopes = {"*", route.route_key, route.model_id}
    if route.alias:
        scopes.add(route.alias)
    if is_effectively_free(route):
        scopes.add("free-daily")
    scopes.add("credits")
    return list(session.scalars(select(QuotaState).where(QuotaState.provider == route.provider,
                                                         QuotaState.scope.in_(sorted(scopes)))))


def quota_exhausted(session: Session, route: ModelRoute) -> QuotaState | None:
    now = utcnow()
    for q in _quota_rows(session, route):
        if q.state == "EXHAUSTED" and (q.resets_at is None or ensure_aware(q.resets_at) > now):  # type: ignore[operator]
            return q
    return None


def _validate_inputs(domain: str, data_classification: str, pool: str) -> None:
    if domain not in {d.value for d in Domain}:
        raise ValidationFailed("domain must be PERSONAL or OFFICE (explicit human choice)", code="DOMAIN_REQUIRED")
    if data_classification not in {c.value for c in DataClassification}:
        raise ValidationFailed(f"unknown data classification {data_classification!r}")
    if pool not in ALL_POOLS:
        raise ValidationFailed(f"unknown pool {pool!r}")


# ------------------------------------------------------------------------------ eligibility


def route_rejections(
    session: Session, route: ModelRoute, *, domain: str, data_classification: str, needs_tools: bool,
    min_context: int | None, office: dict[str, Any] | None = None, office_reason: str = "",
) -> list[str]:
    """All hard-filter reasons that make *route* ineligible (empty list = eligible)."""
    reasons: list[str] = []
    now = utcnow()
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    elif route.config_hash != compute_config_hash(route) and route.status in {"APPROVED", "TRIAL"}:
        reasons.append("configuration changed without a recorded re-review (stored hash differs)")
    if route.mode in {"MOCK", "DEMO"}:
        reasons.append(f"mode {route.mode}: fixtures are never routable")
    status = route.status
    if status == "APPROVED":
        if find_route_decision(session, kind="ROUTE_ACTIVATION", route=route) is None:
            reasons.append("APPROVED without an ACTIVE human ROUTE_ACTIVATION decision bound to the current "
                           "config hash (configuration changed or approval expired): re-activate after review")
    elif status == "TRIAL":
        if find_route_decision(session, kind="TRIAL", route=route) is None:
            reasons.append("TRIAL without an ACTIVE TRIAL decision bound to the current config hash")
    elif status == "DISABLED":
        reasons.append("route DISABLED")
    else:
        reasons.append(f"status {status}: not approved (registry entries are never routable by default)")

    if domain not in (route.eligible_domains or []):
        reasons.append(f"domain {domain} not in eligible_domains {route.eligible_domains or []}")
    if domain == Domain.OFFICE.value:
        if office is None:
            reasons.append(office_reason or "office policy unavailable - OFFICE denied")
        else:
            if office.get("external_worker_allowed") is not True:
                reasons.append("OFFICE policy: external_worker_allowed=false")
            if route.route_key not in (office.get("approved_worker_routes") or []):
                reasons.append("OFFICE policy: route not in approved_worker_routes")

    if data_classification not in (route.allowed_data_classes or []):
        reasons.append(f"data classification {data_classification} not allowed by route data terms "
                       f"(allowed: {route.allowed_data_classes or []})")
    if route.identity_status == "UNVERIFIED" and data_classification != DataClassification.PUBLIC.value:
        reasons.append("model identity UNVERIFIED: only PUBLIC data may be sent")
    if needs_tools and route.supports_tools != "YES":
        reasons.append(f"tool support {route.supports_tools}: tool use not proven for this route")
    if min_context:
        if not route.context_window:
            reasons.append("context window UNKNOWN")
        elif route.context_window < min_context:
            reasons.append(f"context window {route.context_window} < required {min_context}")
    if route.availability == "MISSING":
        reasons.append("route missing from the provider catalog at the last refresh")
    q = quota_exhausted(session, route)
    if q is not None:
        resets = q.resets_at.isoformat() if q.resets_at else "UNKNOWN"
        reasons.append(f"quota EXHAUSTED ({q.scope}, source {q.source}, resets {resets})")
    checked = [ensure_aware(t) for t in (route.pricing_checked_at, route.terms_checked_at)]
    if any(t is None for t in checked):
        reasons.append("pricing/terms never checked")
    elif any(now - t > TERMS_MAX_AGE for t in checked if t is not None):
        reasons.append("pricing/terms older than 30 days: refresh the catalog or re-review terms")
    if not is_effectively_free(route):
        act = find_route_decision(session, kind="ROUTE_ACTIVATION", route=route) or find_route_decision(
            session, kind="TRIAL", route=route)
        budget_id = ((act.details or {}).get("budget_decision_id") if act else None)
        ok, why = budget_decision_valid(session, budget_id, route)
        if not ok:
            label = "free period expired" if route.is_free == "YES" else f"is_free={route.is_free}"
            reasons.append(f"paid route ({label}): {why}")
    return reasons


def _candidates(session: Session, pool: str) -> list[ModelRoute]:
    return list(session.scalars(select(ModelRoute).where(ModelRoute.pool == pool)
                                .order_by(ModelRoute.priority, ModelRoute.route_key)))


def eligible_routes(
    session: Session, *, domain: str, data_classification: str, needs_tools: bool = True,
    min_context: int | None = None, pool: str = "WORKER",
) -> RouteDecision:
    _validate_inputs(domain, data_classification, pool)
    office, office_reason = (_office_policy(session) if domain == Domain.OFFICE.value else (None, ""))
    eligible: list[str] = []
    rejected: dict[str, list[str]] = {}
    for r in _candidates(session, pool):
        reasons = route_rejections(session, r, domain=domain, data_classification=data_classification,
                                   needs_tools=needs_tools, min_context=min_context, office=office,
                                   office_reason=office_reason)
        if reasons:
            rejected[r.route_key] = reasons
        else:
            eligible.append(r.route_key)
    return RouteDecision(route=None, eligible=eligible, rejected=rejected,
                         reason=f"{len(eligible)} eligible, {len(rejected)} rejected (pool {pool}, {domain}, "
                                f"{data_classification})")


# ------------------------------------------------------------------------------ selection


@dataclass(frozen=True)
class RankedRoute:
    route_key: str
    score: float | None
    basis: str  # EVAL | PRIORITY
    priority: int


def eval_score(route: ModelRoute, role: str | None) -> float | None:
    """Score for *role* from recorded eval results, only when they belong to the current config."""
    if not role or not isinstance(route.eval_results, dict):
        return None
    entry = route.eval_results.get(role)
    if not isinstance(entry, dict):
        return None
    if entry.get("config_hash") != route.config_hash:
        return None  # changed configuration: baseline not inherited (§43, §50)
    score = entry.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def rank(session: Session, route_keys: list[str], role: str | None) -> list[RankedRoute]:
    routes = {r.route_key: r for r in session.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(route_keys)))}
    ranked = []
    for key in route_keys:
        r = routes[key]
        s = eval_score(r, role)
        ranked.append(RankedRoute(key, s, "EVAL" if s is not None else "PRIORITY", r.priority))
    ranked.sort(key=lambda x: (0 if x.score is not None else 1, -(x.score or 0.0), x.priority, x.route_key))
    return ranked


def _task_policy(task: TaskSpec) -> dict[str, Any]:
    pol = task.model_route_policy if isinstance(task.model_route_policy, dict) else {}
    pool = str(pol.get("pool") or "WORKER")
    if pool == "SUPERVISOR":
        raise Blocked("worker tasks are never routed to the supervisor pool", code="SUPERVISOR_NOT_ROUTABLE")
    return {
        "pool": pool,
        "needs_tools": bool(pol.get("needs_tools", True)),
        "min_context": pol.get("min_context") if isinstance(pol.get("min_context"), int) else None,
        "routes": [str(x) for x in pol.get("routes") or []],
        "primary": str(pol["primary"]) if pol.get("primary") else None,
    }


def select_route(session: Session, *, task: TaskSpec) -> RouteDecision:
    pol = _task_policy(task)
    decision = eligible_routes(session, domain=task.domain, data_classification=task.data_classification,
                               needs_tools=pol["needs_tools"], min_context=pol["min_context"], pool=pol["pool"])
    eligible = list(decision.eligible)
    rejected = dict(decision.rejected)
    if pol["routes"]:
        for key in [k for k in eligible if k not in pol["routes"]]:
            eligible.remove(key)
            rejected[key] = ["not in the approved TaskSpec route allow-list"]
    role = task.role_key
    if pol["primary"]:
        primary = pol["primary"]
        if primary in eligible:
            route = get_route(session, primary)
            return RouteDecision(route=route, eligible=[primary, *[k for k in eligible if k != primary]],
                                 rejected=rejected, reason=f"primary route {primary} eligible")
        fb = _walk_fallback(session, get_route(session, primary), eligible, rejected)
        if fb is not None:
            return RouteDecision(route=fb, eligible=eligible, rejected=rejected,
                                 reason=f"primary {primary} ineligible; fallback {fb.route_key} along its chain")
        raise Blocked("primary route ineligible and no eligible fallback along its chain - task waits",
                      code="NO_ELIGIBLE_ROUTE", details={"rejected": rejected, "primary": primary})
    if not eligible:
        raise Blocked("no approved route is eligible for this task", code="NO_ELIGIBLE_ROUTE",
                      details={"rejected": rejected, "domain": task.domain,
                               "classification": task.data_classification})
    ranked = rank(session, eligible, role)
    top = get_route(session, ranked[0].route_key)
    return RouteDecision(route=top, eligible=[r.route_key for r in ranked], rejected=rejected,
                         reason=f"selected by {ranked[0].basis.lower()} for role {role}")


def _walk_fallback(session: Session, failed: ModelRoute, eligible: list[str],
                   rejected: dict[str, list[str]]) -> ModelRoute | None:
    if failed.pool == "SUPERVISOR":
        raise Blocked("no supervisor downgrade: the queue waits for the approved supervisor",
                      code="NO_SUPERVISOR_DOWNGRADE")
    for key in failed.fallback_chain or []:
        if key == failed.route_key:
            continue
        target = session.scalars(select(ModelRoute).where(ModelRoute.route_key == key)).first()
        if target is None:
            rejected.setdefault(key, []).append("fallback target not registered")
            continue
        if key not in eligible:
            rejected.setdefault(key, []).append("fallback target is not itself eligible")
            continue
        if target.pool != failed.pool:
            rejected.setdefault(key, []).append(f"fallback crosses pools ({failed.pool} -> {target.pool})")
            continue
        if not is_effectively_free(target):
            rejected.setdefault(key, []).append("paid fallback is never automatic")
            continue
        return target
    return None


def fallback_route(session: Session, *, task: TaskSpec, failed_route_key: str) -> RouteDecision:
    """Next route after *failed_route_key* failed (e.g. 429): along its chain, eligible, free."""
    failed = get_route(session, failed_route_key)
    pol = _task_policy(task)
    decision = eligible_routes(session, domain=task.domain, data_classification=task.data_classification,
                               needs_tools=pol["needs_tools"], min_context=pol["min_context"], pool=failed.pool)
    eligible = [k for k in decision.eligible if not pol["routes"] or k in pol["routes"]]
    rejected = dict(decision.rejected)
    target = _walk_fallback(session, failed, eligible, rejected)
    if target is None:
        raise Blocked("no eligible fallback along the route's chain - back off / queue (no paid fallback)",
                      code="NO_ELIGIBLE_FALLBACK", details={"failed": failed_route_key, "rejected": rejected})
    return RouteDecision(route=target, eligible=eligible, rejected=rejected,
                         reason=f"fallback from {failed_route_key} to {target.route_key}")


# ------------------------------------------------------------------------------ quota


def note_rate_limited(
    session: Session, *, provider: str, scope: str = "free-daily", retry_after_seconds: int | None = None,
    resets_at: datetime | None = None, source: str = "observed-429", details: dict[str, Any] | None = None,
) -> QuotaState:
    """Record an observed 429/limit: the route is excluded until the reset (§50 'Free quota habis')."""
    row = session.scalars(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == scope)).first()
    if row is None:
        row = QuotaState(provider=provider, scope=scope)
        session.add(row)
    now = utcnow()
    if resets_at is None and retry_after_seconds is not None:
        resets_at = now + timedelta(seconds=max(1, int(retry_after_seconds)))
    if resets_at is None and scope == "free-daily":
        resets_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    row.state = "EXHAUSTED"
    row.source = source
    row.observed_at = now
    row.resets_at = resets_at
    row.details = {**(details or {}), "note": "observed limit; backoff/queue, approved free fallback only"}
    session.flush()
    try:
        from aeos.events.service import emit

        emit(session, source="routing", kind="quota.exhausted", entity_type="quota", entity_id=f"{provider}:{scope}",
             payload={"provider": provider, "scope": scope, "resets_at": resets_at.isoformat() if resets_at else None})
    except Exception:  # pragma: no cover
        log.exception("emit quota.exhausted failed")
    return row


# ------------------------------------------------------------------------------ observations


def observe_supervisor_resolution(session: Session, *, route_key: str, requested: str, resolved: str) -> bool:
    """Record the resolved model behind an alias (§12, §23). A change of the resolved model is a
    new configuration: identity UNKNOWN until observed, eval baseline not inherited."""
    route = session.scalars(select(ModelRoute).where(ModelRoute.route_key == route_key)).first()
    if route is None:
        return False
    previous = route.resolved_version
    if route.config_hash is None:
        route.config_hash = compute_config_hash(route)
    route.resolved_version = resolved
    route.resolved_observed_at = utcnow()
    route.identity_status = "VERIFIED"
    if previous and previous not in {"UNKNOWN", resolved}:
        route.notes = (route.notes + "\n" if route.notes else "") + (
            f"{utcnow().date().isoformat()}: alias {requested} now resolves to {resolved} (was {previous})")
        return refresh_config_hash(session, route, reason=f"alias {requested} resolved {previous} -> {resolved}")
    route.config_hash = compute_config_hash(route)
    return False


# ------------------------------------------------------------------------------ activation


def activate_route(
    session: Session, route_key: str, *, actor_user_id: str, mode: str, rationale: str,
    expected_config_hash: str, allowed_data_classes: list[str] | None = None, budget_decision_id: str | None = None,
    expires_in_days: int | None = None, terms_reviewed: bool = False,
) -> tuple[ModelRoute, Decision]:
    """Human activation: records a ROUTE_ACTIVATION (APPROVED) or TRIAL decision bound to the exact
    route config hash. Paid routes also need an explicit BUDGET decision. Never touches OFFICE."""
    if mode not in {"APPROVED", "TRIAL"}:
        raise ValidationFailed("mode must be APPROVED or TRIAL")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("rationale is required (min 10 characters)")
    route = get_route(session, route_key)
    if route.pool == "SUPERVISOR":
        raise ValidationFailed("supervisor routes are governed by the planning policy and the native bridge, "
                               "not by worker activation")
    if route.mode in {"MOCK", "DEMO"}:
        raise ValidationFailed("fixture routes cannot be activated")
    current = compute_config_hash(route)
    if route.config_hash != current:
        refresh_config_hash(session, route, reason="unrecorded configuration change detected at activation")
    if expected_config_hash != route.config_hash:
        raise Conflict("route configuration changed since it was reviewed; reload and review again",
                       code="CONFIG_HASH_MISMATCH", details={"current": route.config_hash})
    if Domain.OFFICE.value in (route.eligible_domains or []):
        raise ValidationFailed("OFFICE eligibility needs a recorded company authorisation (policy module)")
    terms = route.data_terms or {}
    if allowed_data_classes is not None:
        valid = {c.value for c in DataClassification}
        if not allowed_data_classes or any(c not in valid for c in allowed_data_classes):
            raise ValidationFailed(f"allowed_data_classes must be a non-empty subset of {sorted(valid)}")
        restricted = any(terms.get(k) is True for k in RESTRICTED_TERMS_KEYS) or route.identity_status == "UNVERIFIED"
        if restricted and set(allowed_data_classes) - {DataClassification.PUBLIC.value}:
            raise ValidationFailed("data terms/identity of this route allow PUBLIC data only")
        # canonical (enum) order: re-confirming the same classes must not change the reviewed identity
        chosen = set(allowed_data_classes)
        route.allowed_data_classes = [c.value for c in DataClassification if c.value in chosen]
    now = utcnow()
    if terms_reviewed:
        route.terms_checked_at = now
        route.pricing_checked_at = route.pricing_checked_at or now
        route.data_terms = {**terms, "checked_by": "owner"}
    if not is_effectively_free(route):
        ok, why = budget_decision_valid(session, budget_decision_id, route)
        if not ok:
            raise ValidationFailed(f"paid route: {why}", code="BUDGET_DECISION_REQUIRED")
    if mode == "TRIAL":
        days = expires_in_days or TRIAL_DEFAULT_DAYS
        if not 1 <= days <= TRIAL_MAX_DAYS:
            raise ValidationFailed(f"trial expiry must be 1..{TRIAL_MAX_DAYS} days")
    else:
        days = expires_in_days
        if days is not None and not 1 <= days <= APPROVAL_MAX_DAYS:
            raise ValidationFailed(f"approval expiry must be 1..{APPROVAL_MAX_DAYS} days")
    route.config_hash = compute_config_hash(route)
    try:
        from aeos.policy.facade import record_decision

        decision = record_decision(
            session, kind="ROUTE_ACTIVATION" if mode == "APPROVED" else "TRIAL", actor_user_id=actor_user_id,
            target_type=TARGET_TYPE, target_id=route.route_key, target_version=route.config_hash[7:23],
            target_hash=route.config_hash, allowed_operations=["route.use"],
            auth_scope={"domains": [Domain.PERSONAL.value], "data_classes": route.allowed_data_classes,
                        "pool": route.pool},
            domain=Domain.PERSONAL.value, rationale=rationale.strip(),
            expires_at=(now + timedelta(days=days)) if days else None,
            details={"mode": mode, "budget_decision_id": budget_decision_id, "terms_reviewed": terms_reviewed,
                     "previous_status": route.status, "is_free": route.is_free},
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("decision registry (aeos.policy) unavailable - activation cannot be recorded",
                      code="DECISION_REGISTRY_UNAVAILABLE") from exc
    route.status = mode
    session.flush()
    return route, decision


def disable_route(session: Session, route_key: str, *, reason: str) -> ModelRoute:
    route = get_route(session, route_key)
    route.status = "DISABLED"
    try:
        from aeos.policy.facade import invalidate_decisions

        invalidate_decisions(session, target_type=TARGET_TYPE, target_id=route.route_key,
                             reason=f"route disabled: {reason}")
    except (ImportError, ModuleNotFoundError):
        pass  # DISABLED status alone already excludes the route
    session.flush()
    return route
