"""§44 bounded-parallel experiment (topology C) and the resource budget cap (§19, §21, §51 P2).

The pilot runs ONE active implementation worker (§19 "Batas pilot awal"). Two workers are adopted permanently only
by the owner's POLICY decision on workforce/concurrency, and that decision needs a COMPLETED topology-C evaluation
that measurably beat the sequential baseline (``scheduler.set_concurrency``). That evaluation can only be produced if
two workers are allowed to run *for the experiment itself* - otherwise the gate is circular (W6-122). This module is
the narrow, recorded way out of the circle:

* **Experiment grant** - a human EXCEPTION decision bound to ONE RUNNING topology-C EvalRun (its id, suite,
  configuration, conditions and start time). While that run is RUNNING the scheduler's cap is 2 only for TaskSpecs
  bound to that run's slots, and only while every other active worker is also one of them. Every other admission
  rule (write-set conflicts, compute reservation, budget, sandbox, route, verifier) still applies unchanged. The
  grant ends by itself when the run stops RUNNING, is restarted, or the decision expires/is revoked.
* **Parallel-execution evidence** - the bounded-parallel result counts as a two-worker result only when two bound
  attempts really ran with ``caps.concurrency_cap == 2`` and overlapping execution windows; a sequential run
  labelled C never passes the gate.
* **Resource budget** - the owner's ``max_parallel_workers`` (``aeos doctor budget``) is an upper bound on every
  cap; while it is not set the budget is UNKNOWN and the policy/experiment cap applies as before (never guessed).

Deterministic code only; no LLM. Services never commit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import Blocked, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.decisions import Decision

PARALLEL_TOPOLOGY = "C"
EXPERIMENT_KIND = "EXCEPTION"
EXPERIMENT_TARGET_TYPE = "workforce"  # policy.catalog.TARGET_WORKFORCE: written only by the dedicated endpoint
EXPERIMENT_TARGET_PREFIX = "concurrency-experiment:"
EXPERIMENT_OPERATION = "run_two_workers_for_eval_run"
EXPERIMENT_MAX_ACTIVE = 2
EXPERIMENT_MAX_DAYS = 30

SOURCE_PILOT = "PILOT_DEFAULT"
SOURCE_POLICY = "POLICY_DECISION"
SOURCE_EXPERIMENT = "EXPERIMENT_GRANT"


# ----------------------------------------------------------------------------- resource budget (§51 P2)
def budget_max_workers(session: Session) -> tuple[int | None, str]:
    """(max_parallel_workers, note) from the owner's recorded resource budget; None while it is not set."""
    try:
        from aeos.doctor import facade as doctor

        budget = doctor.resource_budget(session)
    except ImportError:
        return None, "resource budget unreadable (doctor module unavailable); UNKNOWN"
    if not budget:
        return None, "resource budget not set by the owner (aeos doctor budget); UNKNOWN, not guessed"
    value = budget.get("max_parallel_workers")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None, "resource budget has no max_parallel_workers; UNKNOWN"
    return value, f"owner resource budget max_parallel_workers={value} (set {budget.get('set_at') or 'UNKNOWN'})"


# ----------------------------------------------------------------------------- bindings of an EvalRun
def bound_task_ids(run: Any) -> list[str]:
    """Every TaskSpec id bound to a slot of *run* (a slot may hold an ordered set of tasks, see
    ``aeos.evaluation.topology``). Invalid bindings stay listed: they were bound by the owner."""
    out: list[str] = []
    for b in getattr(run, "bindings", None) or []:
        ids = b.get("task_ids") if isinstance(b.get("task_ids"), list) else [b.get("task_id")]
        for tid in ids:
            if tid and tid not in out:
                out.append(str(tid))
    return out


def experiment_target_id(eval_run_id: str) -> str:
    return f"{EXPERIMENT_TARGET_PREFIX}{eval_run_id}"


def experiment_binding_hash(run: Any) -> str:
    """The exact experiment the grant authorises: run identity, suite, fixed configuration and conditions, topology
    and start time. A restarted or re-planned run is another experiment (new grant)."""
    started = ensure_aware(getattr(run, "started_at", None))
    return content_hash({"eval_run_id": run.id, "suite_key": run.suite_key,
                         "configuration_hash": run.configuration_hash, "conditions_hash": run.conditions_hash,
                         "topology": run.topology, "started_at": started.isoformat() if started else None,
                         "max_active": EXPERIMENT_MAX_ACTIVE})


def _grant_problems(run: Any) -> list[str]:
    problems: list[str] = []
    if run.status != "RUNNING":
        problems.append(f"eval run {run.id} is {run.status}, not RUNNING")
    if run.topology != PARALLEL_TOPOLOGY:
        problems.append(f"eval run {run.id} tests topology {run.topology or 'none'}, not {PARALLEL_TOPOLOGY} "
                        "(bounded parallel)")
    if run.started_at is None:
        problems.append("eval run has no recorded start")
    if (run.mode or "LIVE") != "LIVE":
        problems.append(f"eval run is {run.mode}; fixture runs never get workers")
    return problems


def active_grant(session: Session, run: Any) -> Decision | None:
    """The ACTIVE, unexpired, human experiment grant bound to exactly this RUNNING run (None otherwise)."""
    target_hash = experiment_binding_hash(run)
    q = (select(Decision).where(Decision.kind == EXPERIMENT_KIND, Decision.target_type == EXPERIMENT_TARGET_TYPE,
                                Decision.target_id == experiment_target_id(run.id), Decision.status == "ACTIVE",
                                Decision.target_hash == target_hash)
         .order_by(Decision.created_at.desc()))
    for d in session.scalars(q):
        if (d.actor_kind or "HUMAN") != "HUMAN":
            continue
        if EXPERIMENT_OPERATION not in (d.allowed_operations or []):
            continue
        exp = ensure_aware(d.expires_at)
        if exp is not None and exp <= utcnow():
            continue
        return d
    return None


def run_domain(run: Any) -> str:
    """The domain of the tasks an EvalRun may bind (a run without a submitted domain is a PERSONAL/platform run)."""
    return str(run.submitted_domain or "PERSONAL")


def grant_experiment(session: Session, *, eval_run_id: str, actor: str, actor_user_id: str, rationale: str,
                     days: int | None, domain: str) -> dict[str, Any]:
    """Owner action: allow two workers ONLY for the TaskSpecs bound to one RUNNING topology-C EvalRun (§19 "Dua worker
    aktif adalah langkah eksperimen berikutnya ... perlu admission/budget yang sah dan bukti integrasi").

    Like every EXCEPTION it carries an explicit expiry chosen by the owner (1..30 days; never defaulted here), and it
    is recorded in the human-chosen *domain*, which must be the domain of the run's tasks (domain-first)."""
    from aeos.audit import service as audit
    from aeos.events import service as events
    from aeos.models.research import EvalRun
    from aeos.workforce import integrations as ext

    if not actor_user_id:
        raise ValidationFailed("the experiment grant is a human owner decision")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("a rationale (min 10 characters) is required", code="RATIONALE_REQUIRED")
    if days is None:
        raise ValidationFailed("an EXCEPTION needs an explicit expiry: choose days (1.."
                               f"{EXPERIMENT_MAX_DAYS})", code="EXPIRY_REQUIRED")
    if not 1 <= int(days) <= EXPERIMENT_MAX_DAYS:
        raise ValidationFailed(f"days must be 1..{EXPERIMENT_MAX_DAYS}")
    run = session.get(EvalRun, eval_run_id)
    if run is None or run_domain(run) != (domain or "").strip().upper():
        raise NotFound("eval run not found in this domain")  # never reveal a run of the other domain
    problems = _grant_problems(run)
    if problems:
        raise Blocked("two workers are granted only for a RUNNING topology-C evaluation run: " + "; ".join(problems),
                      code="EXPERIMENT_NOT_GRANTABLE", details={"reasons": problems, "eval_run_id": eval_run_id})
    budget, budget_note = budget_max_workers(session)
    if budget is not None and budget < EXPERIMENT_MAX_ACTIVE:
        raise Blocked(f"the owner's resource budget allows {budget} parallel worker(s) on this machine "
                      f"({budget_note}); raise it with aeos doctor budget --max-workers first",
                      code="RESOURCE_BUDGET_LIMIT", details={"reasons": [budget_note]})
    target_hash = experiment_binding_hash(run)
    expires = utcnow() + timedelta(days=int(days))
    d = ext.record_decision(
        session, kind=EXPERIMENT_KIND, actor_user_id=actor_user_id, target_type=EXPERIMENT_TARGET_TYPE,
        target_id=experiment_target_id(run.id), target_hash=target_hash,
        allowed_operations=[EXPERIMENT_OPERATION],
        auth_scope={"eval_run_id": run.id, "suite_key": run.suite_key, "topology": run.topology,
                    "applies_to": "TaskSpecs bound to this run's slots, while the run is RUNNING"},
        domain=run_domain(run), rationale=redact_text(rationale)[:2000], expires_at=expires,
        details={"max_active_workers": EXPERIMENT_MAX_ACTIVE, "eval_run_id": run.id, "suite_key": run.suite_key,
                 "configuration_hash": run.configuration_hash, "conditions_hash": run.conditions_hash,
                 "budget": budget_note,
                 "unchanged_checks": ["write-set conflicts", "compute reservation", "budget", "sandbox", "route",
                                      "verifier"],
                 "rollback": {"how": "revoke this decision or stop the eval run (aeos eval finish); the pilot cap "
                                     "of 1 applies again at the next admission"}})
    audit.record(session, actor_type="user", actor_id=actor, action="workforce.concurrency.experiment_grant",
                 target_type="eval_run", target_id=run.id, domain=run_domain(run),
                 reason=redact_text(rationale)[:500], details={"decision_id": d.id, "target_hash": target_hash})
    events.emit(session, source="controller", kind="scheduler.concurrency_experiment_granted",
                entity_type="eval_run", entity_id=run.id, domain=run_domain(run),
                payload={"decision_id": d.id, "max_active": EXPERIMENT_MAX_ACTIVE})
    return {"eval_run_id": run.id, "decision_id": d.id, "target_hash": target_hash,
            "max_active": EXPERIMENT_MAX_ACTIVE, "expires_at": expires.isoformat(), "domain": run_domain(run),
            "bound_tasks": len(bound_task_ids(run)), "budget": budget_note}


def experiment_for_task(session: Session, task: Any, *, other_active_task_ids: list[str]) -> dict[str, Any]:
    """Whether an experiment grant lets *task* run as one of two workers now: {"applies": bool, "reason", ...}."""
    from aeos.models.research import EvalRun

    task_id = getattr(task, "id", None)
    if not task_id:
        return {"applies": False, "reason": "no task"}
    for run in session.scalars(select(EvalRun).where(EvalRun.status == "RUNNING",
                                                     EvalRun.topology == PARALLEL_TOPOLOGY)):
        ids = bound_task_ids(run)
        if task_id not in ids:
            continue
        if getattr(task, "domain", None) != run_domain(run):
            return {"applies": False, "reason": "task domain differs from the evaluation run's domain"}
        problems = _grant_problems(run)
        d = active_grant(session, run) if not problems else None
        if d is None:
            return {"applies": False, "eval_run_id": run.id,
                    "reason": "; ".join(problems) or f"no ACTIVE owner experiment grant bound to eval run {run.id}"}
        outside = [t for t in other_active_task_ids if t not in ids]
        if outside:
            return {"applies": False, "eval_run_id": run.id, "decision_id": d.id,
                    "reason": f"another active worker ({len(outside)}) is not part of eval run {run.id}; the "
                              "experiment never adds a worker next to unrelated work"}
        return {"applies": True, "eval_run_id": run.id, "decision_id": d.id, "cap": EXPERIMENT_MAX_ACTIVE,
                "reason": f"owner experiment grant {d.id} for RUNNING topology-C eval run {run.id}"}
    return {"applies": False, "reason": "task is not bound to a RUNNING topology-C evaluation run"}


# ----------------------------------------------------------------------------- parallel-execution evidence
def _window(r: Any) -> tuple[Any, Any] | None:
    a, b = ensure_aware(r.started_at), ensure_aware(r.finished_at)
    return (a, b) if a is not None and b is not None and b > a else None  # type: ignore[operator]


def parallel_execution_evidence(session: Session, run: Any) -> dict[str, Any]:
    """§44 bounded parallel is a claim about EXECUTION: PROVEN only when at least two implementation attempts of
    tasks bound to *run* were admitted with ``caps.concurrency_cap == 2`` and their recorded execution windows
    overlapped. Computed from WorkerRun records (runtime timestamps), never from a label or a worker report."""
    from aeos.models.workforce import WorkerRun

    ids = bound_task_ids(run)
    if not ids:
        return {"status": "NOT_PROVEN", "problems": ["no task is bound to the evaluation run"], "cap2_runs": 0,
                "overlaps": []}
    rows = [r for r in session.scalars(select(WorkerRun).where(WorkerRun.task_id.in_(ids)))
            if (r.run_type or "IMPLEMENTATION") == "IMPLEMENTATION" and (r.mode or "LIVE") == "LIVE"]
    cap2 = [r for r in rows if int((r.caps or {}).get("concurrency_cap") or 1) == EXPERIMENT_MAX_ACTIVE
            and _window(r) is not None]
    overlaps: list[dict[str, Any]] = []
    for i, a in enumerate(cap2):
        for b in cap2[i + 1:]:
            if a.task_id == b.task_id:
                continue
            wa, wb = _window(a), _window(b)
            assert wa is not None and wb is not None
            start, end = max(wa[0], wb[0]), min(wa[1], wb[1])
            if end > start:
                overlaps.append({"runs": [a.id, b.id], "tasks": [a.task_id, b.task_id],
                                 "overlap_s": round((end - start).total_seconds(), 3)})
    problems: list[str] = []
    if len(cap2) < 2:
        problems.append(f"only {len(cap2)} bound implementation attempt(s) ran with concurrency cap 2 "
                        "(a sequential run labelled C is not a bounded-parallel result)")
    if not overlaps:
        problems.append("no two bound attempts overlapped in time: two workers never ran at once")
    return {"status": "PROVEN" if not problems else "NOT_PROVEN", "problems": problems, "cap2_runs": len(cap2),
            "overlaps": overlaps[:20], "bound_tasks": len(ids)}


def experiment_view(session: Session, run: Any) -> dict[str, Any]:
    """Read-only state of the experiment for one EvalRun (UI/CLI)."""
    d = active_grant(session, run) if not _grant_problems(run) else None
    recorded = d or session.scalar(select(Decision).where(Decision.kind == EXPERIMENT_KIND,
                                                          Decision.target_type == EXPERIMENT_TARGET_TYPE,
                                                          Decision.target_id == experiment_target_id(run.id))
                                   .order_by(Decision.created_at.desc()))
    return {"eval_run_id": run.id, "topology": run.topology, "status": run.status,
            "grant_decision_id": recorded.id if recorded is not None else None, "grant_active": d is not None,
            "grantable": not _grant_problems(run),
            "problems": _grant_problems(run), "bound_tasks": len(bound_task_ids(run)),
            "parallel_execution": parallel_execution_evidence(session, run)}
