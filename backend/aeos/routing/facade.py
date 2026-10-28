"""Cross-module contract for worker model routing (§23). Owned by ``aeos.routing``.

Two-stage routing: hard eligibility filter first (domain/data/terms/tools/context/budget/
availability), then selection by evaluated task results. Models never select themselves; fallback
only to already-approved routes; no silent paid fallback or supervisor downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute


@dataclass(frozen=True)
class RouteDecision:
    route: ModelRoute | None
    eligible: list[str]
    rejected: dict[str, list[str]] = field(default_factory=dict)  # route_key -> reasons
    reason: str = ""
    # additive (wave 7): the RUNNING EvalRun whose exact route was selected for a TaskSpec bound to one of its slots
    eval_run_id: str | None = None
    # additive (ADR-0128): why a per-token paid route was admitted (``reason`` ESCALATION or OVERFLOW), else None
    paid_admission: dict[str, Any] | None = None


def eligible_routes(
    session: Session, *, domain: str, data_classification: str, needs_tools: bool = True,
    min_context: int | None = None, pool: str = "WORKER",
) -> RouteDecision:
    from aeos.routing.service import eligible_routes as impl

    return impl(session, domain=domain, data_classification=data_classification, needs_tools=needs_tools,
                min_context=min_context, pool=pool)


def select_route(session: Session, *, task: TaskSpec, preferred_routes: list[str] | None = None) -> RouteDecision:
    """Raise ``Blocked`` when no approved route is eligible for the task. ``preferred_routes`` (additive, ADR-0124
    contract (a)): the implementing agent's ``route_policy.preferred_routes``; it only re-orders eligible routes."""
    from aeos.routing.service import select_route as impl

    if preferred_routes:
        return impl(session, task=task, preferred_routes=list(preferred_routes))
    return impl(session, task=task)


# --- additive contracts (existing signatures above are unchanged) ------------------------------


def fallback_route(session: Session, *, task: TaskSpec, failed_route_key: str) -> RouteDecision:
    """Next route after a failure (e.g. 429): only along the failed route's fallback_chain, only to
    routes that are themselves eligible, same pool and free. Raises ``Blocked`` -> back off/queue."""
    from aeos.routing.service import fallback_route as impl

    return impl(session, task=task, failed_route_key=failed_route_key)


def terms_eligibility(route: ModelRoute, *, data_classification: str) -> list[str]:
    """Terms/data/provider eligibility reasons of *route* for a workload of *data_classification* (empty =
    eligible). The evaluation gate of §40/§43; a subset of the routing hard filter."""
    from aeos.routing.service import terms_eligibility as impl

    return impl(route, data_classification=data_classification)


def route_use_problem(session: Session, route: ModelRoute) -> str | None:
    """Why *route* may not serve a request right now (None = allowed): its APPROVED/TRIAL status must
    rest on an ACTIVE human decision bound to the CURRENT configuration hash, and a route that is not
    free (any more) needs a valid budget decision. The broker re-checks this per request (§23)."""
    from aeos.routing.service import route_use_problem as impl

    return impl(session, route)


def client_restriction_problem(route: ModelRoute) -> str | None:
    """Why the broker may not call *route* as its own HTTP client (None = no provider client restriction, or the owner
    recorded a decision for it at activation; additive, ADR-0041 amendment 2026-10-25). Part of
    ``route_use_problem``; the broker also checks it first to report ``CLIENT_TERMS_RESTRICTED``."""
    from aeos.routing.service import client_restriction_problem as impl

    return impl(route)


def note_rate_limited(
    session: Session, *, provider: str, scope: str = "free-daily", retry_after_seconds: int | None = None,
    source: str = "observed-429",
) -> None:
    """Record an observed rate/quota limit so routing excludes the route until it resets."""
    from aeos.routing.service import note_rate_limited as impl

    impl(session, provider=provider, scope=scope, retry_after_seconds=retry_after_seconds, source=source)


def observe_supervisor_call(session: Session, *, model: str, engine_version: str | None,
                            effort: str | None = None) -> dict[str, object]:
    """§23: bind the Claude Code version, the effort/thinking setting, the planning prompt revision, the policy version
    and the planning context strategy of a planning/review call to the supervisor route's configuration identity (a
    later change is a new configuration, never silent)."""
    from aeos.routing.service import observe_supervisor_call as impl

    return impl(session, model=model, engine_version=engine_version, effort=effort)


def supervisor_configuration(session: Session, model: str) -> dict[str, object] | None:
    """§02: the supervisor route configuration a call ran on and its evaluation state (NEEDS_EVALUATION etc.)."""
    from aeos.routing.service import supervisor_configuration as impl

    return impl(session, model)


def free_worker_problem(session: Session, route: ModelRoute, *, run_id: str | None = None) -> str | None:
    """Decision C (additive, ADR-0041 amendment 2026-10-26): why a free OpenRouter WORKER route may not serve this
    request (None = not such a route, or allowed) - its eligibility rules and, with *run_id*, that the run's TaskSpec
    is PUBLIC (an unreadable TaskSpec is refused). The broker checks it on every request."""
    from aeos.routing.service import free_worker_problem as impl

    return impl(session, route, run_id=run_id)


def free_tier_limit_problem(session: Session, route: ModelRoute) -> dict[str, object] | None:
    """The account-wide OpenRouter free-model limit (requests/minute, requests/UTC day, an observed 429) that refuses a
    call of *route* now: ``code`` FREE_TIER_LIMIT_REACHED, ``reason``, ``retry_after_seconds``; None when the route is
    not an OpenRouter ``:free`` model or no limit is reached (additive, decision C)."""
    from aeos.routing.free_tier import route_limit_problem as impl

    return impl(session, route)


def note_free_tier_rate_limited(session: Session, *, route_key: str, retry_after_seconds: int | None,
                                headers: dict[str, str] | None = None, run_id: str | None = None,
                                source: str = "observed-429") -> dict[str, object]:
    """Record a 429 OpenRouter answered for a ``:free`` model as an account-wide quota state (every free route waits;
    additive, decision C). ``retry_after_seconds`` None = the provider sent no Retry-After."""
    from aeos.routing.free_tier import note_rate_limited as impl

    return impl(session, route_key=route_key, retry_after_seconds=retry_after_seconds, headers=headers, run_id=run_id,
                source=source)


def free_tier_count_lock(session: Session) -> bool:
    """Take the write lock that makes counting an OpenRouter ``:free`` request and recording it one atomic step (held
    until the caller commits; additive, decision C). False when it cannot be taken: the caller refuses."""
    from aeos.routing.free_tier import acquire_count_lock as impl

    return impl(session)


# --- tool-call probe evidence (additive, ADR-0125, owner decision 2026-10-28) ------------------------------------
# The broker's tool-call probe (``aeos broker probe-route --tools``) records PROBE evidence with these marks and routing
# reads them back (``tool_capable``): one definition for the writer and the reader.
TOOL_PROBE_EVIDENCE_KIND = "PROBE"
TOOL_PROBE_COLLECTOR = "aeos-broker/route-tool-probe"
TOOL_PROBE_MARK = "TOOL_CALL"  # the evidence environment's ``probe`` value
TOOL_PROBE_PASS = "PASS"
TOOL_PROBE_FAIL = "FAIL"
TOOL_PROBE_ANSWERED = "REACHABLE"  # the probe outcome of an answer the model actually gave (only those decide)


def tool_capable(session: Session, route: ModelRoute) -> bool:
    """Contract (b) of 2026-10-28 (ADR-0125): *route* counts as tool-capable when ``supports_tools`` is YES, or when
    a trusted tool-call probe recorded a PASS bound to the route's CURRENT configuration hash. A configuration change
    (a new hash) makes older probe evidence irrelevant. The probe never changes ``supports_tools`` (a CONFIG_FIELD).
    Routing's hard filter uses it wherever a task needs tools."""
    from aeos.routing.service import tool_capable as impl

    return impl(session, route)


def tool_probe_recorded(session: Session, *, route_key: str, verdict: str, evidence_id: str | None) -> None:
    """The broker recorded a tool-call probe of *route_key* (additive, ADR-0125): routing's eligibility may have
    changed, so the dependent readiness checks go STALE and ``route.tool_probe`` is emitted (the one emit point)."""
    from aeos.routing.service import tool_probe_recorded as impl

    impl(session, route_key=route_key, verdict=verdict, evidence_id=evidence_id)


def unstored_credential_ref(reason: str) -> str | None:
    """The secret reference a ``credential <ref> not stored: aeos secrets set <ref>`` rejection names (additive,
    2026-10-28): the key the route needs is not in the secret store. None for any other reason."""
    from aeos.routing.service import unstored_credential_ref as impl

    return impl(reason)


def store_unavailable_credential_ref(reason: str) -> str | None:
    """The secret reference a ``credential <ref> cannot be checked: secret store unavailable`` rejection names
    (additive, 2026-10-28): the store could not say whether the route's key is stored. None for any other reason."""
    from aeos.routing.service import store_unavailable_credential_ref as impl

    return impl(reason)


def withhold_private_refs(session: Session, payload: Any, *, domain: str) -> Any:
    """*payload* with every route credential reference name that a viewer of *domain* without any project scope may
    not see replaced by its label (additive, 2026-10-28; L12): ``office/`` names outside OFFICE and every
    ``projects/<id>/`` name. The one rule of the route views (``service.withheld_refs``/``withhold_refs``), for a
    platform-level payload every session reads (the Doctor's route rows)."""
    from sqlalchemy import select

    from aeos.routing.service import withheld_refs, withhold_refs

    routes = session.scalars(select(ModelRoute)).all()
    return withhold_refs(payload, withheld_refs(routes, domain=domain, project_visible=None))


# --- load-aware best-first selection (additive, ADR-0124, owner decision 2026-10-28) ----------------------------------


def zero_marginal(route: ModelRoute) -> bool:
    """The route costs no money at the margin: effectively free, a loopback LOCAL route, or a SUBSCRIPTION route with
    trusted plan terms (usage within the plan is allowance). A re-selection between attempts may only land on one."""
    from aeos.routing.capacity import zero_marginal as impl

    return impl(route)


def route_capacity(session: Session, *, task: TaskSpec, route: ModelRoute) -> dict[str, Any]:
    """§19 admission answer for the selected route: its concurrency cap, the active tasks on it and - for a
    SUBSCRIPTION route - the 5h-window allowance fit with the estimate the run holds (``hold``). Route-level figures:
    they span domains."""
    from aeos.routing.capacity import route_capacity as impl

    return impl(session, task=task, route=route)


def paid_admission(session: Session, *, task: TaskSpec, route: ModelRoute) -> dict[str, Any] | None:
    """ADR-0128 (additive): whether the per-token paid *route* may take *task* - ``admitted`` with ``reason``
    ESCALATION or OVERFLOW, else ``code`` PAID_NOT_ADMITTED and ``why``. None for a route that is not per-token paid
    (or a task bound to an evaluation slot). Read only."""
    from aeos.routing.paid import paid_admission as impl

    return impl(session, task, route)


def reselect_allowed(session: Session, route_key: str) -> bool:
    """Between attempts: a task stopped at a quota on *route_key* may move to another ACTIVE $0-marginal route of the
    same data class only when the expected wait for its route is known and longer than the owner setting
    ``route_reselect_after_minutes``. A running attempt is never moved."""
    from aeos.routing.capacity import reselect_allowed as impl

    return impl(session, route_key)


def route_cooldown(session: Session, *, provider: str, route_key: str,
                   reported_seconds: int | None = None) -> dict[str, Any]:
    """The cool-down a new 429/5xx on *route_key* starts (read only): ``streak``, ``seconds``, ``basis`` (DOUBLING or
    PROVIDER_RESET) and ``resets_at``. The broker writes it with its quota-state record of the 429."""
    from aeos.routing.capacity import cooldown_for as impl

    return impl(session, provider=provider, route_key=route_key, reported_seconds=reported_seconds)


def note_route_cooldown(session: Session, *, provider: str, route_key: str, status: int,
                        reported_seconds: int | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Record a cool-down after a provider 5xx on *route_key* (new tasks skip the route until it ends; a running attempt
    stays on it)."""
    from aeos.routing.capacity import note_route_cooldown as impl

    return impl(session, provider=provider, route_key=route_key, status=status, reported_seconds=reported_seconds,
                run_id=run_id)


def scope_route_keys(auth_scope: object) -> list[str]:
    """The exact route keys a decision's ``auth_scope.routes`` names (a list, or a ',' separated text); test
    membership in this list, never in the raw value. The one implementation lives in ``aeos.routing.service``."""
    from aeos.routing.service import scope_route_keys as impl

    return impl(auth_scope)
