"""Fail-closed calls into other modules' facades.

Other modules are developed in parallel; when an implementation module is missing the facade's
lazy import raises ``ImportError``. Every wrapper here turns that into an explicit NOT-READY
answer with a reason — it never pretends success (AGENTS.md invariant 8).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Capability, Domain
from aeos.core.errors import AeosError, Blocked
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.decisions import Decision
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.routing import ModelRoute

log = logging.getLogger("aeos.workforce")


def _unavailable(module: str, exc: BaseException) -> str:
    log.info("module %s unavailable: %s", module, exc)
    return f"{module} module unavailable ({type(exc).__name__}); failing closed"


def approved_version(session: Session, plan_id: str | None) -> tuple[PlanVersion | None, str]:
    if not plan_id:
        return None, "task has no plan"
    from aeos.planning import facade as planning

    try:
        v = planning.get_approved_version(session, plan_id)
    except ImportError as exc:
        return None, _unavailable("planning", exc)
    if v is None:
        return None, "plan has no ACTIVE approval decision"
    return v, ""


def acceptance_valid(session: Session, task: TaskSpec) -> tuple[bool, str]:
    from aeos.verification import facade as verification

    try:
        ok = verification.acceptance_is_valid(session, task.id, plan_version_hash=task.plan_version_hash)
    except ImportError as exc:
        return False, _unavailable("verification", exc)
    return bool(ok), "" if ok else "no ACCEPTED acceptance record still valid for this plan version"


def acceptance_valid_for_run(session: Session, task: TaskSpec, base_commit: str | None) -> tuple[bool, str]:
    from aeos.verification import facade as verification

    try:
        ok = verification.acceptance_is_valid(session, task.id, base_commit=base_commit,
                                              plan_version_hash=task.plan_version_hash)
    except ImportError as exc:
        return False, _unavailable("verification", exc)
    return bool(ok), "" if ok else "acceptance record is not ACCEPTED/valid for this base/plan identity"


def documentation_blockers(session: Session, task: TaskSpec, version: PlanVersion) -> list[tuple[str, str]]:
    """§41 readiness criteria: the documentation sections required before implementation (PROJECT_CREATION and
    the task's FEATURE_APPROVAL PRD/test plan) must be complete; otherwise the task WAITS (fail closed: an
    unavailable docs engine is a wait too, never a silent pass)."""
    from aeos.core.errors import AeosError
    from aeos.docs_engine import facade as docs

    try:
        pending = docs.stage_blockers(session, version, task_id=task.id, feature_key=task.feature_key)
    except ImportError as exc:
        return [("DOCS_READINESS_UNVERIFIED", _unavailable("docs_engine", exc))]
    except AeosError as exc:
        return [("DOCS_READINESS_UNVERIFIED", f"{exc.code}: {exc.message}"[:300])]
    except Exception as exc:  # a readiness check that cannot complete is a wait, never a pass (fail closed)
        log.warning("documentation readiness check failed: %s", type(exc).__name__)
        return [("DOCS_READINESS_UNVERIFIED", f"documentation readiness could not be evaluated ({type(exc).__name__})")]
    if not pending:
        return []
    text = "; ".join(f"{p['logical_doc_id']} ({', '.join(p['sections'])})" for p in pending[:6])
    return [("DOCS_NOT_READY", f"required documentation still pending before implementation (§41): {text}"
                               + (f" (+{len(pending) - 6} more)" if len(pending) > 6 else ""))]


def capability_ready(session: Session, capability: Capability, *, domain: str, project_id: str | None,
                     exclude_checks: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    from aeos.doctor import facade as doctor

    try:
        if exclude_checks:
            doctor.require_capability(session, capability, domain=domain, project_id=project_id,
                                      exclude_checks=exclude_checks)
        else:
            doctor.require_capability(session, capability, domain=domain, project_id=project_id)
    except ImportError as exc:
        return False, [_unavailable("doctor", exc)]
    except Blocked as exc:
        reasons = list(exc.details.get("reasons") or []) or [exc.message]
        return False, [f"{capability.value}: {r}" for r in reasons]
    return True, []


def eval_served(session: Session, task: TaskSpec, route: ModelRoute) -> bool:
    """Whether *route* (an eval-only pool) is the exact route of a RUNNING evaluation *task* is bound to."""
    try:
        from aeos.broker.facade import EVAL_POOLS
        from aeos.evaluation import facade as evaluation
    except ImportError:
        return False
    if route.pool not in EVAL_POOLS:
        return False
    return evaluation.eval_serving(session, task=task, route_key=route.route_key).get("status") == "SERVED"


# Routing refusals that are a quota wait, not an ineligible route (decision C, ADR-0041 amendment 2026-10-26): the
# free OpenRouter WORKER route is at its account-wide rate/daily limit and the task waits for it (no paid fallback).
# ADR-0124 (2026-10-28): ROUTE_CAPACITY - every $0-marginal route is busy, cooling down or out of 5h allowance and
# the task waits (never a paid route).
QUOTA_ROUTE_CODES = frozenset({"FREE_TIER_LIMIT_REACHED", "ROUTE_CAPACITY"})


def select_route(session: Session, task: TaskSpec) -> tuple[ModelRoute | None, list[str]]:
    route, reasons, _code = select_route_detail(session, task)
    return route, reasons


def select_route_detail(session: Session, task: TaskSpec, *,
                        preferred_routes: list[str] | None = None) -> tuple[ModelRoute | None, list[str], str]:
    """``select_route`` plus the admission wait code when no route is returned: ``ROUTE_INELIGIBLE``, or a routing
    quota code (``QUOTA_ROUTE_CODES``) when the task waits for a rate-limited free route or for route capacity.
    ``preferred_routes``: the implementing agent's ``route_policy.preferred_routes`` (it only re-orders eligible
    routes, ADR-0124 contract (a))."""
    from aeos.routing import facade as routing

    try:
        decision = (routing.select_route(session, task=task, preferred_routes=preferred_routes) if preferred_routes
                    else routing.select_route(session, task=task))
    except ImportError as exc:
        return None, [_unavailable("routing", exc)], "ROUTE_INELIGIBLE"
    except Blocked as exc:
        code = exc.code if exc.code in QUOTA_ROUTE_CODES else "ROUTE_INELIGIBLE"
        return None, [exc.message, *[str(r) for r in exc.details.get("reasons", [])]], code
    route, problems = _checked_route(session, task, decision.route, decision.reason)
    return route, problems, "ROUTE_INELIGIBLE"


def free_tier_limit(session: Session, route: ModelRoute) -> str | None:
    """The account-wide OpenRouter free-model limit that keeps *route* waiting now (None = none, or not a free
    OpenRouter model). Routing unavailable -> an OpenRouter route waits (fail closed)."""
    try:
        from aeos.routing import facade as routing

        limit = routing.free_tier_limit_problem(session, route)
    except (ImportError, AttributeError) as exc:
        free = route.provider == "openrouter" and str(route.model_id or "").endswith(":free")
        return _unavailable("routing", exc) if free else None
    return str(limit.get("reason") or "free-model limit reached") if limit else None


def _checked_route(session: Session, task: TaskSpec, route: ModelRoute | None,
                   reason: str) -> tuple[ModelRoute | None, list[str]]:
    if route is None:
        return None, [reason or "no eligible approved route"]
    problems: list[str] = []
    # Defence in depth (§12, §23): the worker pool never uses the Claude Max supervisor route, and
    # the route must explicitly list the task's domain. A non-WORKER route is accepted only as the exact route of a
    # RUNNING evaluation this task is bound to (eval-only ARENA path, §49 M6), re-checked here independently.
    if route.provider == "claude-native" or (route.pool != "WORKER" and not eval_served(session, task, route)):
        problems.append(f"route {route.route_key} is not a WORKER route")
    if route.status not in {"APPROVED", "TRIAL"}:
        problems.append(f"route {route.route_key} status {route.status} is not APPROVED/TRIAL")
    if task.domain not in (route.eligible_domains or []):
        problems.append(f"route {route.route_key} is not eligible for domain {task.domain}")
    if problems:
        return None, problems
    return route, []


def office_external_allowed(session: Session) -> tuple[bool, str]:
    from aeos.policy import facade as policy

    try:
        pol = policy.office_policy(session)
    except ImportError as exc:
        return False, _unavailable("policy", exc)
    if not pol.get("external_worker_allowed"):
        return False, "OFFICE external_worker_allowed=false (master s23 default deny)"
    return True, ""


def office_route_authorised(session: Session, route: ModelRoute) -> tuple[bool, str]:
    """§23: an OFFICE task runs on a route only while the owner's office_route decision (bound to the route
    identity and the ACTIVE company authorisation) holds. Policy module absent -> denied (fail closed)."""
    try:
        from aeos.policy.office_routes import office_route_authorised as impl

        decision = impl(session, route)
    except (ImportError, AttributeError) as exc:  # policy module (or one of its seams) missing: denied
        return False, _unavailable("policy", exc)
    if decision is None:
        return False, (f"route {route.route_key} has no ACTIVE OFFICE eligibility decision bound to its identity and "
                       "the current company authorisation (POST /api/policy/office/routes)")
    return True, ""


def reserve(session: Session, *, run_id: str, project_id: str, domain: str, route_key: str | None,
            est_usd: float, est_tokens: int, est_requests: int) -> Any:
    """Raises ``Blocked`` when the reservation is refused or the budget module is unavailable."""
    from aeos.cost import facade as cost

    try:
        return cost.reserve(session, cost.ReservationRequest(
            run_id=run_id, project_id=project_id, domain=domain, route_key=route_key, est_usd=est_usd,
            est_tokens=est_tokens, est_requests=est_requests))
    except ImportError as exc:
        raise Blocked(_unavailable("cost", exc), code="BUDGET_UNAVAILABLE") from exc


def subscription_terms(route: ModelRoute) -> tuple[dict[str, Any] | None, str | None]:
    """(terms, problem) of a flat-fee SUBSCRIPTION route (ADR-0080 amendment 2026-10-26); (None, None) otherwise."""
    from aeos.cost import facade as cost

    try:
        return cost.subscription_terms(route)
    except ImportError as exc:
        if (route.pricing or {}).get("billing") == "SUBSCRIPTION":
            return None, _unavailable("cost", exc)
        return None, None


def subscription_allowance(session: Session, route: ModelRoute) -> dict[str, Any] | None:
    """Plan + allowance per provider window of a SUBSCRIPTION route (None for any other route); UNKNOWN when the cost
    module cannot count it (fail closed)."""
    from aeos.cost import facade as cost

    try:
        return cost.allowance_status(session, route)
    except ImportError as exc:
        if (route.pricing or {}).get("billing") == "SUBSCRIPTION":
            return {"route_key": route.route_key, "state": "UNKNOWN", "problems": [_unavailable("cost", exc)],
                    "windows": []}
        return None


def route_capacity(session: Session, task: TaskSpec, route: ModelRoute) -> dict[str, Any]:
    """The selected route's capacity answer (cap, active tasks, allowance fit and the estimate the run holds;
    ADR-0124). Routing unavailable -> ``UNAVAILABLE`` and no hold (the admission checks stay in force)."""
    try:
        from aeos.routing import facade as routing

        return routing.route_capacity(session, task=task, route=route)
    except (ImportError, AttributeError) as exc:
        return {"route_key": route.route_key, "status": "UNAVAILABLE", "reason": _unavailable("routing", exc),
                "hold": None}


PAID_NOT_ADMITTED = "PAID_NOT_ADMITTED"


def paid_admission(session: Session, task: TaskSpec, route: ModelRoute) -> dict[str, Any] | None:
    """ADR-0128: the admission re-check of a selected per-token paid route (``admitted`` with ``reason`` ESCALATION or
    OVERFLOW, else ``code``/``why``); None for a route that is not per-token paid. Routing unavailable or the check
    failing -> a route that looks paid (not free, not a SUBSCRIPTION, not a LOCAL route) is not admitted (fail
    closed)."""
    try:
        from aeos.routing import facade as routing

        return routing.paid_admission(session, task=task, route=route)
    except (ImportError, AttributeError, AeosError) as exc:
        looks_paid = (route.is_free != "YES" and (route.pricing or {}).get("billing") != "SUBSCRIPTION"
                      and not (route.pool == "LOCAL" and route.provider == "local"))
        if not looks_paid:
            return None
        why = exc.message if isinstance(exc, AeosError) else _unavailable("routing", exc)
        return {"route_key": route.route_key, "admitted": False, "reason": None, "code": PAID_NOT_ADMITTED,
                "why": f"paid route {route.route_key} cannot be checked for ESCALATION or OVERFLOW ({why}); the task "
                       "waits (fail closed, ADR-0128)"}


def zero_marginal(route: ModelRoute) -> bool:
    """The route costs no money at the margin (free, loopback LOCAL, a SUBSCRIPTION within its plan). Routing
    unavailable -> False (fail closed: a re-selection then waits)."""
    try:
        from aeos.routing import facade as routing

        return bool(routing.zero_marginal(route))
    except (ImportError, AttributeError):
        return False


def reselect_allowed(session: Session, route_key: str) -> bool:
    """A quota-stopped task may leave *route_key* between attempts (expected wait above the owner threshold, ADR-0124).
    Routing unavailable -> False: the task keeps waiting for its route, as before."""
    try:
        from aeos.routing import facade as routing

        return bool(routing.reselect_allowed(session, route_key))
    except (ImportError, AttributeError):
        return False


def subscription_block(status: dict[str, Any] | None) -> tuple[str, str] | None:
    """(wait code, reason without allowance figures) when a subscription route's state refuses admission: a call billed
    beyond the plan, the 5h window used up, or an uncountable call in it (advisory week/month windows never block)."""
    if not status:
        return None
    from aeos.cost import facade as cost

    try:
        return cost.subscription_admission_block(status)
    except ImportError as exc:
        return "SUBSCRIPTION_ALLOWANCE_UNKNOWN", _unavailable("cost", exc)


def subscription_without_figures(status: dict[str, Any]) -> dict[str, Any]:
    """The allowance status for a principal who cannot see every domain: no window figures (GLOBAL/ROUTE rule)."""
    from aeos.cost import facade as cost

    try:
        return cost.subscription_without_figures(status)
    except ImportError:
        return {"state": status.get("state"), "plan": status.get("plan"), "windows": None, "spend_hidden": True}


def release(session: Session, reservation_id: str | None) -> str:
    if not reservation_id:
        return "NONE"
    from aeos.cost import facade as cost

    try:
        cost.release(session, reservation_id)
    except ImportError as exc:
        _unavailable("cost", exc)
        return "UNKNOWN"
    except Exception as exc:  # recorded, never hidden
        log.warning("reservation release failed: %s", exc)
        return "FAILED"
    return "RELEASED"


def record_decision(session: Session, **kwargs: Any) -> Decision:
    from aeos.policy import facade as policy

    try:
        return policy.record_decision(session, **kwargs)
    except ImportError as exc:
        raise Blocked(_unavailable("policy", exc), code="POLICY_UNAVAILABLE") from exc


def active_exception_decision(session: Session, *, target_type: str, target_id: str,
                               target_hash: str | None = None) -> Decision | None:
    """ACTIVE, unexpired, human EXCEPTION decision for a target (policy facade first, then a
    read-only lookup of the canonical Decision rows when the policy module is absent). With
    ``target_hash`` only a decision bound to exactly that hash counts."""
    from sqlalchemy import select

    from aeos.policy import facade as policy

    try:
        d = policy.find_active_decision(session, kind="EXCEPTION", target_type=target_type, target_id=target_id,
                                        target_hash=target_hash)
    except ImportError:
        d = None
    if d is None or (target_hash is not None and d.target_hash != target_hash):
        # Read-only lookup of the canonical rows (module absent, or the facade returned another binding).
        q = select(Decision).where(Decision.kind == "EXCEPTION", Decision.target_type == target_type,
                                   Decision.target_id == target_id, Decision.status == "ACTIVE")
        if target_hash is not None:
            q = q.where(Decision.target_hash == target_hash)
        d = next(iter(session.scalars(q.order_by(Decision.created_at.desc()))), None)
    if d is None or d.status != "ACTIVE" or d.actor_kind != "HUMAN":
        return None
    exp = ensure_aware(d.expires_at)
    if exp is not None and exp <= utcnow():
        return None
    return d


def is_personal(domain: str) -> bool:
    return domain == Domain.PERSONAL.value


def verifier_available() -> tuple[bool, str]:
    """§19 admission: "siapa verifier dan penerima hasilnya". Without the verification module no
    result can ever be accepted, so a worker is not started (fail closed, no wasted run/cost)."""
    import importlib

    try:
        importlib.import_module("aeos.verification.acceptance")
    except ImportError as exc:
        return False, _unavailable("verification", exc)
    return True, ""


QUARANTINE_TARGET_TYPE = "run_quarantine"


def quarantine_release(session: Session, run: Any) -> Decision | None:
    """ACTIVE human EXCEPTION decision releasing a quarantined patch after special review (§21),
    bound to this run AND its exact runtime head commit (a different patch needs a new decision)."""
    if not getattr(run, "head_commit", None):
        return None
    d = active_exception_decision(session, target_type=QUARANTINE_TARGET_TYPE, target_id=run.id,
                                  target_hash=run.head_commit)
    if d is None or d.target_hash != run.head_commit:
        return None
    return d
