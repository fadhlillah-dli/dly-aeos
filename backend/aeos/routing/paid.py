"""Per-token paid routes: ESCALATION and OVERFLOW admission (ADR-0128; owner decision R34 of 2026-10-28).

The owner's words (2026-10-28): "Lakukan yang terbaik; saya tidak ingin hanya membuang $10 di OpenRouter dan $10 di
OpenCode", with the answers that bound this module: paid OpenRouter is used by ESCALATION (after 2 failed $0-marginal
attempts of the task) AND OVERFLOW (when every $0-marginal route for the role is at capacity / allowance fit /
cool-down); PUBLIC tasks are never paid (they have the free fallback).

A *per-token paid* route is one whose use costs money per call: not effectively free, not a loopback LOCAL route and
not a flat-fee SUBSCRIPTION route (OpenCode Go). ``service.rank_tier`` ranks it below every $0-marginal route, so an eval
score or an agent preference re-orders it only among paid routes. ``paid_admission`` then decides whether a paid route
may take an INTERNAL task at all:

* ``ESCALATION``: the task has at least ``ESCALATION_AFTER`` finished attempts on $0-marginal routes that failed for a
  quality reason (``quality_failures``): the run ended FAILED and the verifier said FAIL, the review returned findings
  (``REVIEW_FINDINGS``, the outcome ``request_repair`` gives a reviewed attempt after a verifier FAIL or reviewer
  findings) or the acceptance was REJECTED. A quota, time-out, resource, lease, shutdown, cancel, reassign, policy,
  blocker, scope or non-zero-exit stop is not a quality failure. While escalation is active the paid routes are tried
  first (``select_route``); a busy paid route still falls back to the $0-marginal ones.
* ``OVERFLOW``: every $0-marginal route eligible for the task (its role's allow-list, data class, domain, tools) is
  blocked right now by a load skip (``ROUTE_BUSY``, ``ALLOWANCE_FIT``) or a limit (cool-down, free-tier limit), and at
  least one such route exists.

Anything else waits as before (``ROUTE_CAPACITY`` after a skip, else ``PAID_NOT_ADMITTED``). A paid route still passes
every earlier gate: eligibility (ACTIVE decision bound to its configuration, the owner's BUDGET decision covering the
route, data terms, domain), the TaskSpec cost cap (``limits.max_cost_usd``, else ``COST_UNKNOWN``) and the GLOBAL budget
reservation at lease time. The owner switches each path off with the settings ``paid_escalation`` and
``paid_overflow`` (both on by default). An admitted paid run is recorded at lease time (event ``route.paid_admitted``
and an audit record, ``WorkerRun.caps.paid_admission``).

Pure reads. Nothing here changes a route's configuration identity, the worker prompt revision or the policy version.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataClassification
from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun

ESCALATION = "ESCALATION"
OVERFLOW = "OVERFLOW"
CODE_NOT_ADMITTED = "PAID_NOT_ADMITTED"
EVENT_KIND = "route.paid_admitted"
AUDIT_ACTION = "routing.route.paid_admitted"
ESCALATION_AFTER = 2  # the owner's number (R34): two failed $0-marginal attempts of the task

# A finished attempt failed for quality when its run ended FAILED and one of these holds (the verifier's own
# vocabulary, ADR-0060, plus the older PASSED/FAILED words ``scheduler.update_run`` still accepts).
QUALITY_OUTCOMES = frozenset({"REVIEW_FINDINGS"})
QUALITY_VERIFIER = frozenset({"FAIL", "FAILED"})
QUALITY_ACCEPTANCE = frozenset({"REJECTED"})
# Outcomes that are never a quality failure, whatever another field says (provider quota, time, resources, lease,
# controller, owner/operator actions, a worker blocker, a scope stop, a non-zero exit).
NOT_QUALITY_OUTCOMES = frozenset({"QUOTA_EXHAUSTED", "CONTROLLER_SHUTDOWN", "LEASE_LOST", "ORPHANED_ALIVE", "TIMEOUT",
                                  "RESOURCE_LIMIT", "CANCELLED", "REASSIGNED", "POLICY_CHANGED", "WORKER_BLOCKER",
                                  "SCOPE_VIOLATION", "EXIT_NONZERO"})


def _settings() -> Any:
    from aeos.core.config import get_settings

    return get_settings()


def per_token_paid(route: ModelRoute) -> bool:
    """The route costs money per call: it needs a BUDGET decision and is not a flat-fee SUBSCRIPTION route."""
    from aeos.routing.service import is_subscription, needs_budget

    return needs_budget(route) and not is_subscription(route)


def is_quality_failure(run: WorkerRun) -> bool:
    if run.execution_status != "FAILED" or (run.outcome_code or "") in NOT_QUALITY_OUTCOMES:
        return False
    return ((run.outcome_code or "") in QUALITY_OUTCOMES or (run.verifier_status or "") in QUALITY_VERIFIER
            or (run.acceptance_status or "") in QUALITY_ACCEPTANCE)


def quality_failures(session: Session, task: TaskSpec) -> list[str]:
    """Ids of the task's finished LIVE implementation attempts on $0-marginal routes that failed for quality."""
    from aeos.routing import capacity

    if not task.id:
        return []
    runs = list(session.scalars(select(WorkerRun).where(
        WorkerRun.task_id == task.id, WorkerRun.mode == "LIVE", WorkerRun.run_type == "IMPLEMENTATION",
        WorkerRun.execution_status == "FAILED").order_by(WorkerRun.created_at)))
    keys = sorted({str(r.model_route_key) for r in runs if r.model_route_key})
    routes = ({r.route_key: r for r in session.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(keys)))}
              if keys else {})
    out: list[str] = []
    for run in runs:
        route = routes.get(str(run.model_route_key))
        if route is not None and capacity.zero_marginal(route) and is_quality_failure(run):
            out.append(run.id)
    return out


def escalation_active(session: Session, task: TaskSpec) -> bool:
    """INTERNAL, the owner setting on, and at least ``ESCALATION_AFTER`` quality failures on $0-marginal routes."""
    if task.data_classification != DataClassification.INTERNAL.value or not _settings().paid_escalation:
        return False
    return len(quality_failures(session, task)) >= ESCALATION_AFTER


def zero_marginal_state(session: Session, task: TaskSpec) -> tuple[list[str], list[str]]:
    """(blocked, open): the $0-marginal routes eligible for *task* (its allow-list included) that a load skip or a
    limit keeps out now, and those that could take it now."""
    from aeos.routing import capacity, free_tier
    from aeos.routing import service as svc

    pol = svc._task_policy(task)
    decision, limited = svc._eligibility(session, domain=task.domain, data_classification=task.data_classification,
                                         needs_tools=pol["needs_tools"], min_context=pol["min_context"],
                                         pool=pol["pool"], tools=[str(t) for t in task.tools_allowed or []])
    allowed = pol["routes"]
    runs = [r for r in capacity.in_flight_runs(session) if r.task_id != task.id]
    blocked: list[str] = []
    open_: list[str] = []
    for key in decision.eligible:
        if allowed and key not in allowed:
            continue
        route = svc.get_route(session, key)
        if not capacity.zero_marginal(route):
            continue
        (open_ if capacity.capacity_skip(session, route, task=task, runs=runs) is None else blocked).append(key)
    for key in limited:
        if allowed and key not in allowed:
            continue
        route = svc.get_route(session, key)
        if capacity.zero_marginal(route) or free_tier.is_decision_c_route(route):
            blocked.append(key)
    return blocked, open_


def _eval_bound(session: Session, task: TaskSpec) -> bool:
    from aeos.routing.service import _eval_serving

    return _eval_serving(session, task).get("status") != "NOT_BOUND"


def paid_admission(session: Session, task: TaskSpec, route: ModelRoute, *,
                   blocked: list[str] | None = None) -> dict[str, Any] | None:
    """Whether the per-token paid *route* may take *task* (None: not a per-token paid route, or a task bound to an
    evaluation slot, which its own EvalRun governs). *blocked*: the $0-marginal routes the caller already saw blocked
    after it tried every one of them (``select_route``); without it the state is read here (admission re-check)."""
    if not per_token_paid(route) or _eval_bound(session, task):
        return None
    settings = _settings()
    key = route.route_key
    base: dict[str, Any] = {"route_key": key, "data_classification": task.data_classification,
                            "escalation_enabled": bool(settings.paid_escalation),
                            "overflow_enabled": bool(settings.paid_overflow), "quality_failures": [],
                            "blocked_zero_marginal": [], "open_zero_marginal": []}
    if task.data_classification != DataClassification.INTERNAL.value:
        return {**base, "admitted": False, "reason": None, "code": CODE_NOT_ADMITTED,
                "why": f"paid route {key}: a {task.data_classification} task never runs on a per-token paid route "
                       "(ADR-0128: paid routes take INTERNAL tasks only; PUBLIC work has the free fallback)"}
    failures = quality_failures(session, task)
    base["quality_failures"] = failures
    if settings.paid_escalation and len(failures) >= ESCALATION_AFTER:
        return {**base, "admitted": True, "reason": ESCALATION, "code": None,
                "why": f"paid route {key}: ESCALATION after {len(failures)} attempts on $0-marginal routes that "
                       "failed for quality (verifier, review or acceptance)"}
    if blocked is None:
        blocked, open_ = zero_marginal_state(session, task)
    else:
        open_ = []
    base.update(blocked_zero_marginal=list(blocked), open_zero_marginal=list(open_))
    if settings.paid_overflow and blocked and not open_:
        return {**base, "admitted": True, "reason": OVERFLOW, "code": None,
                "why": f"paid route {key}: OVERFLOW, every $0-marginal route eligible for this task is busy, out of "
                       f"its 5h allowance fit or cooling down ({len(blocked)} route(s))"}
    esc = (f"{len(failures)} of {ESCALATION_AFTER} quality failures" if settings.paid_escalation
           else "escalation switched off (setting paid_escalation)")
    if not settings.paid_overflow:
        ovf = "overflow switched off (setting paid_overflow)"
    elif open_:
        ovf = f"{len(open_)} $0-marginal route(s) can take the task"
    else:
        ovf = "no $0-marginal route is eligible for this task, so none is blocked"
    return {**base, "admitted": False, "reason": None, "code": CODE_NOT_ADMITTED,
            "why": f"paid route {key} not admitted (ADR-0128): no ESCALATION ({esc}) and no OVERFLOW ({ovf}); "
                   "the task waits"}
