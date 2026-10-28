"""Load-aware best-first selection (ADR-0124; owner decision R31 of 2026-10-28).

The owner's words (2026-10-28): "best models first, the others after; use LiveBench or other accountable sources; the
best available models run in parallel, matched to each role's needs". ``service.select_route`` ranks the routes that
pass every hard filter (tier -> eval -> the agent's ``preferred_routes`` position -> priority -> key) and then takes the
first one this module does not SKIP for load. A skip is never a failure: the next ranked route is tried, and after a
skip only a route with no marginal cost (free, a loopback LOCAL route, or a SUBSCRIPTION route within its plan) may be
chosen - or, since ADR-0128, a per-token paid route that ``paid.paid_admission`` admits for an INTERNAL task (OVERFLOW:
every $0-marginal route is skipped or limited; ESCALATION: two quality failures); otherwise the task waits
(``ROUTE_CAPACITY``).

Skips (the reason code leads the rejection text; a reason never carries allowance figures, they span domains):

The skips apply to every selection, an evaluation-bound task included (it waits for its evaluated route, never moves),
and admission re-checks them for the selected route (``route_capacity`` -> ``wait``).

* ``ROUTE_BUSY``: the route is at its concurrency cap (setting ``route_max_active``; default 1 for a SUBSCRIPTION route
  with a monthly allowance of at most 30 USD, else 2), every SUBSCRIPTION route of the provider together is at
  ``go_max_active_total``, or every OpenRouter ``:free`` route together is at ``openrouter_free_max_active`` (one
  account-wide rate limit). The scheduler's global cap (HARD_MAX_ACTIVE) stays in force on top.
* ``ALLOWANCE_FIT``: a SUBSCRIPTION route admits a task only when the task's estimated allowance fits the trailing 5h
  window: 20% of the monthly allowance minus the usage this AEOS instance recorded minus the holds of its in-flight runs
  (equal fits). The estimate: the median allowance of this instance's last finished runs of the same role on the route
  (at least ``MIN_HISTORY_RUNS``), else the LiveBench prior below, else half the route's 5h window (``DEFAULT``). An
  estimate larger than the route's whole 5h window is capped at that window (``capped_at_window``): such a task is
  admitted only on an empty window (no usage, no holds), so a route whose prior exceeds its window (the Pro Go models)
  still serves and builds the history that replaces the prior, instead of being skipped forever. The admitted run holds its estimate (``WorkerRun.caps.allowance_hold``) until it is no longer in flight; what it used is
  in the usage ledger, so the hold shrinks by it. A window AEOS cannot count (``UNKNOWN``) is skipped too (fail closed).
  No calendar-month pace guard: the week and month windows stay advisory (ADR-0080 amendment 2026-10-26).
* ``PUBLIC_SHARE``: a PUBLIC task fits only into ``go_public_share`` of the route's 5h window minus what PUBLIC runs
  used and hold in it (decision C': PUBLIC work runs on Go first and must not starve INTERNAL tasks, which are not
  limited by the share).

Cool-down: a 429 or a provider 5xx records a cool-down on the route's own ``QuotaState`` (scope = route key; written by
the broker through the cost module or ``service.note_rate_limited``): 60 s, doubling per repeat while the previous
cool-down ended less than 30 minutes ago, at most 30 minutes - or longer when the provider's reset (Retry-After) says
so. A new cool-down never shortens a reset already recorded on that QuotaState (a 429's longer Retry-After stays when
a 5xx follows; ``EXISTING_RESET``). A route in cool-down is a "limit" rejection, so it is skipped for NEW tasks only; a running attempt stays pinned to
its route (the broker does not refuse its requests). Between attempts, a task the executor stopped at a quota moves to
another route only when the expected wait for its route exceeds ``route_reselect_after_minutes`` (``reselect_allowed``)
and only to an ACTIVE $0-marginal route of the same data class (the scheduler checks it at admission).

Pure reads except ``note_route_cooldown``. Nothing here changes a route's configuration identity (``CONFIG_FIELDS``),
the worker prompt revision or the policy version: no ACTIVE TRIAL is voided by these rules or their settings.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataClassification
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import free_tier

ROUTE_BUSY = "ROUTE_BUSY"
ALLOWANCE_FIT = "ALLOWANCE_FIT"
PUBLIC_SHARE = "PUBLIC_SHARE"
SKIP_CODES = (ROUTE_BUSY, ALLOWANCE_FIT, PUBLIC_SHARE)
CODE_CAPACITY = "ROUTE_CAPACITY"  # Blocked code: no $0-marginal route is free to take the task now; it waits

IN_FLIGHT = ("LEASED", "RUNNING")
SMALL_ALLOWANCE_USD = Decimal(30)
DEFAULT_ROUTE_CAP = 2
SMALL_ROUTE_CAP = 1
HISTORY_RUNS = 10
MIN_HISTORY_RUNS = 3
DEFAULT_WINDOW_SHARE = Decimal("0.5")
WINDOW = "5h"
COOLDOWN_BASE_SECONDS = 60
COOLDOWN_MAX_SECONDS = 30 * 60
USD_Q = Decimal("0.01")

# Prior allowance of one task per Go model (USD, rounded up to the cent): LiveBench 2.0 release 2026-10-25, cost.csv,
# column long_agentic divided by nq_long_agentic (18 tasks) = the cost of one long agentic task at the model's list
# prices, which equal the Go allowance rates of these models. A long agentic task is the heaviest LiveBench task, so the
# prior is conservative for ordinary tasks. Used only until this instance has its own history for the route and role.
PRIOR_SOURCE = ("LiveBench 2.0 release 2026-10-25, https://livebench.ai cost.csv: long_agentic / nq_long_agentic "
                "(cost of one long agentic task at list prices), read 2026-10-28")
ALLOWANCE_PRIORS_USD: dict[str, Decimal] = {
    "kimi-k3": Decimal("18.00"),  # 323.8289 / 18
    "step-5-preview": Decimal("9.06"),  # 162.9821 / 18
    "kimi-k3.1-pro": Decimal("7.43"),  # 133.6662 / 18
    "glm-5.3-flash": Decimal("1.63"),  # 29.3179 / 18
    "deepseek-v4.1-flash": Decimal("1.45"),  # 25.9631 / 18
    "mimo-v2.6-pro": Decimal("1.22"),  # 21.8646 / 18
    "minimax-m3.1-flash": Decimal("1.09"),  # 19.4973 / 18
}


def _settings() -> Any:
    from aeos.core.config import get_settings

    return get_settings()


def _usd(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _out(d: Decimal) -> str:
    return str(d.quantize(USD_Q, rounding=ROUND_CEILING))


# ------------------------------------------------------------------------------ cost classes


def zero_marginal(route: ModelRoute) -> bool:
    """A route whose use costs no money at the margin: effectively free, a loopback LOCAL route, or a SUBSCRIPTION route
    with trusted plan terms (usage within the plan is allowance, not money; Extra Usage OFF is the owner's guard)."""
    from aeos.routing.service import is_effectively_free, is_local_loopback, is_subscription

    if is_effectively_free(route) or is_local_loopback(route):
        return True
    if not is_subscription(route):
        return False
    from aeos.cost import facade as cost

    terms, _problem = cost.subscription_terms(route)
    return terms is not None


def go_first(route: ModelRoute) -> bool:
    """Decision C' (owner, 2026-10-28): a SUBSCRIPTION route within its plan ($0 marginal) ranks first for PUBLIC tasks,
    ahead of the free OpenRouter routes, which stay the fallback."""
    from aeos.routing.service import is_subscription

    return is_subscription(route) and zero_marginal(route)


# ------------------------------------------------------------------------------ in-flight runs and caps


def in_flight_runs(session: Session) -> list[WorkerRun]:
    """LIVE runs that hold a route now (leased or running)."""
    return list(session.scalars(select(WorkerRun).where(
        WorkerRun.execution_status.in_(IN_FLIGHT), WorkerRun.mode == "LIVE", WorkerRun.model_route_key.is_not(None))))


def _routes_of(session: Session, runs: list[WorkerRun]) -> dict[str, ModelRoute]:
    keys = sorted({str(r.model_route_key) for r in runs if r.model_route_key})
    if not keys:
        return {}
    return {r.route_key: r for r in session.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(keys)))}


def route_cap(route: ModelRoute, settings: Any = None) -> tuple[int, str]:
    """(cap, basis) of concurrent tasks on *route*: the owner setting, else the default."""
    from aeos.routing.service import is_subscription

    settings = settings or _settings()
    override = (settings.route_max_active or {}).get(route.route_key)
    if override:
        return int(override), "setting route_max_active"
    if is_subscription(route):
        allowance = _usd((route.pricing or {}).get("allowance_usd_month"))
        if allowance is None or allowance <= SMALL_ALLOWANCE_USD:
            return SMALL_ROUTE_CAP, "default for a subscription route with a monthly allowance of at most 30 USD"
    return DEFAULT_ROUTE_CAP, "default"


def busy_problem(session: Session, route: ModelRoute, runs: list[WorkerRun], settings: Any = None) -> str | None:
    """Why *route* cannot take one more task now (None = it can): its own cap, the provider-wide subscription cap, the
    account-wide OpenRouter ``:free`` cap."""
    from aeos.routing.service import is_subscription

    settings = settings or _settings()
    key = route.route_key
    on_route = [r for r in runs if r.model_route_key == key]
    cap, basis = route_cap(route, settings)
    if len(on_route) >= cap:
        return f"{len(on_route)} active task(s) on route {key}; its cap is {cap} ({basis})"
    by_key = _routes_of(session, runs)
    if is_subscription(route):
        same = [r for r in runs if (rt := by_key.get(str(r.model_route_key))) is not None
                and rt.provider == route.provider and is_subscription(rt)]
        total = int(settings.go_max_active_total)
        if len(same) >= total:
            return (f"{len(same)} active task(s) on the {route.provider} subscription routes; their total cap is "
                    f"{total} (setting go_max_active_total)")
    if free_tier.is_free_model_route(route):
        free = [r for r in runs if (rt := by_key.get(str(r.model_route_key))) is not None
                and free_tier.is_free_model_route(rt)]
        total = int(settings.openrouter_free_max_active)
        if len(free) >= total:
            return (f"{len(free)} active task(s) on OpenRouter :free routes; the account-wide cap is {total} "
                    "(setting openrouter_free_max_active)")
    return None


# ------------------------------------------------------------------------------ allowance fit


def _upstream_model(route: ModelRoute) -> str:
    mp = route.mode_params if isinstance(route.mode_params, dict) else {}
    return str(mp.get("upstream_model") or str(route.model_id or "").split("/", 1)[-1])


def allowance_estimate(session: Session, route: ModelRoute, role: str | None, *,
                       window_limit: Decimal | None = None) -> dict[str, Any]:
    """The allowance (USD) one task of *role* is expected to use on *route*, with its basis: LEDGER_HISTORY, PRIOR or
    DEFAULT (see the module docstring), capped at *window_limit* when it is larger (``capped_at_window``)."""
    est = _estimate(session, route, role, window_limit=window_limit)
    if window_limit is not None and window_limit > 0 and Decimal(est["usd"]) > window_limit:
        est = {**est, "usd": _out(window_limit), "capped_at_window": True, "uncapped_usd": est["usd"],
               "source": f"{est['source']}; capped at the route's whole 5h window (admitted only on an empty window)"}
    return est


def _estimate(session: Session, route: ModelRoute, role: str | None, *,
              window_limit: Decimal | None = None) -> dict[str, Any]:
    if role:
        rows = session.scalars(select(WorkerRun).where(
            WorkerRun.model_route_key == route.route_key, WorkerRun.role_key == role, WorkerRun.mode == "LIVE",
            WorkerRun.execution_status.not_in((*IN_FLIGHT, "QUEUED"))).order_by(WorkerRun.created_at.desc()).limit(50))
        samples: list[Decimal] = []
        for r in rows:
            v = _usd((r.usage_summary or {}).get("allowance_usd"))
            if v is not None and v >= 0:
                samples.append(v)
            if len(samples) >= HISTORY_RUNS:
                break
        if len(samples) >= MIN_HISTORY_RUNS:
            return {"usd": _out(Decimal(statistics.median(samples))), "basis": "LEDGER_HISTORY", "samples": len(samples),
                    "source": f"median allowance of the last {len(samples)} finished runs of role {role} on this route "
                              "(this AEOS instance's records)"}
    prior = ALLOWANCE_PRIORS_USD.get(_upstream_model(route))
    if prior is not None:
        return {"usd": _out(prior), "basis": "PRIOR", "samples": 0, "source": PRIOR_SOURCE}
    limit = window_limit if window_limit is not None else Decimal(0)
    return {"usd": _out(limit * DEFAULT_WINDOW_SHARE), "basis": "DEFAULT", "samples": 0,
            "source": "no history and no prior: half of the route's 5h allowance window (conservative default)"}


def _hold(session: Session, run: WorkerRun, route: ModelRoute, window_limit: Decimal) -> Decimal:
    """What an in-flight run still holds: its recorded estimate (else one computed now) minus the allowance it used."""
    caps = run.caps if isinstance(run.caps, dict) else {}
    hold = caps.get("allowance_hold")
    usd = _usd(hold.get("usd")) if isinstance(hold, dict) else None
    if usd is None:
        usd = Decimal(allowance_estimate(session, route, run.role_key, window_limit=window_limit)["usd"])
    used = _usd((run.usage_summary or {}).get("allowance_usd")) or Decimal(0)
    return max(Decimal(0), usd - used)


def _public_runs(session: Session, run_ids: list[str]) -> set[str]:
    ids = sorted({i for i in run_ids if i})
    if not ids:
        return set()
    rows = session.execute(select(WorkerRun.id).join(TaskSpec, TaskSpec.id == WorkerRun.task_id).where(
        WorkerRun.id.in_(ids), TaskSpec.data_classification == DataClassification.PUBLIC.value))
    return {str(r[0]) for r in rows}


def _public_used(session: Session, route: ModelRoute, since: datetime) -> Decimal:
    """Allowance recorded in the window by calls of runs whose task is PUBLIC."""
    rows = list(session.execute(select(UsageLedgerEntry.run_id, UsageLedgerEntry.details).where(
        UsageLedgerEntry.route_key == route.route_key, UsageLedgerEntry.category == "WORKER_API",
        UsageLedgerEntry.mode == "LIVE", UsageLedgerEntry.at >= since)))
    public = _public_runs(session, [str(r[0]) for r in rows if r[0]])
    total = Decimal(0)
    for run_id, details in rows:
        if run_id and str(run_id) in public:
            total += _usd((details or {}).get("allowance_usd")) or Decimal(0)
    return total


def allowance_fit(session: Session, route: ModelRoute, *, role: str | None, data_classification: str,
                  runs: list[WorkerRun], settings: Any = None, now: datetime | None = None) -> dict[str, Any] | None:
    """The 5h-window fit of one more task on a SUBSCRIPTION route (None for any other route). ``skip`` is
    (code, reason) when the task does not fit, else None. Figures stay in the result, never in the reason."""
    from aeos.cost import facade as cost
    from aeos.routing.service import is_subscription

    if not is_subscription(route):
        return None
    settings = settings or _settings()
    status = cost.allowance_status(session, route, now=now)
    if status is None:
        return None
    key = route.route_key
    out: dict[str, Any] = {"route_key": key, "state": status.get("state"), "skip": None}
    if status.get("state") in ("UNTRUSTED", "BEYOND_PLAN"):
        return out  # the admission check refuses these with their own codes (an owner action, never silently skipped)
    window = next((w for w in status.get("windows") or [] if w.get("window") == WINDOW), None)
    if window is None:
        return out
    limit = _usd(window.get("limit_usd")) or Decimal(0)
    used = _usd(window.get("used_usd")) or Decimal(0)
    est = allowance_estimate(session, route, role, window_limit=limit)
    need = Decimal(est["usd"])
    on_route = [r for r in runs if r.model_route_key == key]
    holds = {r.id: _hold(session, r, route, limit) for r in on_route}
    held = sum(holds.values(), Decimal(0))
    remaining = limit - used - held
    out.update({"estimate": est, "window": WINDOW, "limit_usd": _out(limit), "used_usd": _out(used),
                "held_usd": _out(held), "remaining_usd": str(remaining.quantize(USD_Q))})
    if status.get("state") == "UNKNOWN":
        out["skip"] = (ALLOWANCE_FIT, f"route {key}: a call in its 5h allowance window cannot be counted, so the fit "
                                      "of this task is unknown (fail closed; at most 5 hours)")
        return out
    if need > remaining:
        out["skip"] = (ALLOWANCE_FIT, f"route {key}: the estimated allowance of this task (basis {est['basis']}) does "
                                      "not fit the remaining 5h window as counted by this AEOS instance (usage and "
                                      "in-flight holds); the next route is tried")
        return out
    if data_classification == DataClassification.PUBLIC.value:
        share = Decimal(str(settings.go_public_share))
        current = ensure_aware(now or utcnow())
        assert current is not None
        public_ids = _public_runs(session, list(holds))
        public_used = _public_used(session, route, current - timedelta(hours=5))
        public_held = sum((h for rid, h in holds.items() if rid in public_ids), Decimal(0))
        public_remaining = limit * share - public_used - public_held
        out.update({"public_share": str(share), "public_used_usd": _out(public_used),
                    "public_held_usd": _out(public_held), "public_remaining_usd": str(public_remaining.quantize(USD_Q))})
        if need > public_remaining:
            out["skip"] = (PUBLIC_SHARE, f"route {key}: PUBLIC tasks have used their share of its 5h window (setting "
                                         "go_public_share); INTERNAL tasks are not limited by it")
    return out


# ------------------------------------------------------------------------------ the skip decision


def capacity_skip(session: Session, route: ModelRoute, *, task: TaskSpec, runs: list[WorkerRun],
                  settings: Any = None) -> tuple[str, str] | None:
    """(code, reason) when *route* must be skipped for *task* now because of load; None = it may take the task."""
    settings = settings or _settings()
    busy = busy_problem(session, route, runs, settings)
    if busy:
        return ROUTE_BUSY, busy
    fit = allowance_fit(session, route, role=task.role_key, data_classification=task.data_classification, runs=runs,
                        settings=settings)
    if fit is not None and fit.get("skip"):
        code, why = fit["skip"]
        return str(code), str(why)
    return None


def route_capacity(session: Session, *, task: TaskSpec, route: ModelRoute) -> dict[str, Any]:
    """The §19 admission answer for the selected route: its cap, the active tasks on it and - for a SUBSCRIPTION route -
    the allowance fit with the estimate the run will hold (``hold``). Figures span domains (route-level). ``wait``
    ({code, reason}, the reason without figures) when the route cannot take the task now (busy, allowance fit, PUBLIC
    share): admission enforces it for every selection, an evaluation-bound task included."""
    settings = _settings()
    runs = [r for r in in_flight_runs(session) if r.task_id != task.id]
    cap, basis = route_cap(route, settings)
    out: dict[str, Any] = {"route_key": route.route_key, "cap": cap, "cap_basis": basis,
                           "active_on_route": sum(1 for r in runs if r.model_route_key == route.route_key),
                           "zero_marginal": zero_marginal(route), "hold": None, "wait": None}
    if busy_problem(session, route, runs, settings) is not None:
        out["wait"] = {"code": CODE_CAPACITY, "reason": f"{ROUTE_BUSY}: route {route.route_key} or its provider group "
                                                        "is at its concurrency cap; the task waits - never a paid route"}
    fit = allowance_fit(session, route, role=task.role_key, data_classification=task.data_classification, runs=runs,
                        settings=settings)
    if fit is not None:
        skip = fit.pop("skip", None)
        out["allowance_fit"] = {**fit, "fits": skip is None, "skip_code": skip[0] if skip else None}
        est = fit.get("estimate")
        if isinstance(est, dict):
            out["hold"] = {"usd": est.get("usd"), "basis": est.get("basis")}
        if skip is not None and out["wait"] is None:
            out["wait"] = {"code": CODE_CAPACITY, "reason": f"{skip[0]}: route {route.route_key} cannot take this task "
                                                            "within its 5h window now (allowance fit or PUBLIC share); "
                                                            "the task waits - never a paid route"}
    return out


# ------------------------------------------------------------------------------ cool-down


def cooldown_for(session: Session, *, provider: str, route_key: str, reported_seconds: int | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    """The cool-down a new 429/5xx on *route_key* starts: 60 s doubling per repeat (the previous cool-down of the route
    ended less than 30 minutes ago, or is still running) up to 30 minutes, or the provider's longer reset; never shorter
    than a reset still recorded on the route's QuotaState (``EXISTING_RESET``)."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    row = session.scalar(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == route_key))
    streak = 1
    prev = (row.details or {}).get("cooldown") if row is not None else None
    ended = ensure_aware(row.resets_at) if row is not None else None
    if isinstance(prev, dict) and ended is not None and current - ended <= timedelta(seconds=COOLDOWN_MAX_SECONDS):
        try:
            streak = max(1, int(prev.get("streak") or 0)) + 1
        except (TypeError, ValueError):
            streak = 1
    doubling = min(COOLDOWN_BASE_SECONDS * 2 ** (min(streak, 16) - 1), COOLDOWN_MAX_SECONDS)
    seconds, basis = doubling, "DOUBLING"
    if isinstance(reported_seconds, int) and not isinstance(reported_seconds, bool) and reported_seconds > doubling:
        seconds, basis = reported_seconds, "PROVIDER_RESET"
    resets_at = current + timedelta(seconds=seconds)
    if row is not None and row.state == "EXHAUSTED" and ended is not None and ended > resets_at:
        # a longer reset already recorded on the route (a 429's Retry-After) is never cut by a later 429/5xx
        seconds, basis, resets_at = math.ceil((ended - current).total_seconds()), "EXISTING_RESET", ended
    return {"streak": streak, "seconds": seconds, "basis": basis, "resets_at": resets_at.isoformat()}


def note_route_cooldown(session: Session, *, provider: str, route_key: str, status: int,
                        reported_seconds: int | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Record a cool-down after a provider 5xx (a 429 is recorded by the broker's quota write with ``cooldown_for``)."""
    from aeos.routing.service import note_rate_limited

    cd = cooldown_for(session, provider=provider, route_key=route_key, reported_seconds=reported_seconds)
    note_rate_limited(session, provider=provider, scope=route_key, resets_at=datetime.fromisoformat(cd["resets_at"]),
                      source="observed-5xx" if status >= 500 else "observed-429",
                      details={"status": status, "run_id": run_id, "cooldown": cd})
    return cd


def expected_wait(session: Session, route_key: str, *, now: datetime | None = None) -> tuple[timedelta | None, bool]:
    """(longest wait, known) for the route's own quota/cool-down rows and - for an OpenRouter ``:free`` model - the
    account's free-model limit. (None, True) = nothing to wait for; (None, False) = a reset time is UNKNOWN."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    waits: list[timedelta] = []
    for q in session.scalars(select(QuotaState).where(QuotaState.scope == route_key, QuotaState.state == "EXHAUSTED")):
        resets = ensure_aware(q.resets_at)
        if resets is None:
            return None, False
        if resets > current:
            waits.append(resets - current)
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is not None and free_tier.is_free_model_route(route):
        problem = free_tier.route_limit_problem(session, route, now=current)
        if problem is not None:
            seconds = problem.get("retry_after_seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                return None, False
            waits.append(timedelta(seconds=float(seconds)))
    return (max(waits) if waits else None), True


def reselect_allowed(session: Session, route_key: str, *, now: datetime | None = None) -> bool:
    """Between attempts only: whether a task stopped at a quota on *route_key* may move to another route instead of
    waiting - the expected wait is known and longer than ``route_reselect_after_minutes``. The admission check then
    accepts only an ACTIVE $0-marginal route of the same data class (``zero_marginal``)."""
    wait, known = expected_wait(session, route_key, now=now)
    if not known or wait is None:
        return False
    return wait > timedelta(minutes=int(_settings().route_reselect_after_minutes))
