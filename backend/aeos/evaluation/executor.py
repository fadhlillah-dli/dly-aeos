"""Evaluation execution: case slots, task bindings and the trusted collector (§43 protocol, §49 M6).

How an EvalRun is executed in this build, without a second scheduler (U-12):

1. ``start_run`` (owner): PLANNED -> RUNNING. The suite must still be intact (content hash) and the
   registered route must still have exactly the configuration the run was planned for. Every case x
   repetition becomes a slot.
2. Each slot is executed as an ordinary TaskSpec of an approved plan through the workforce — admission,
   cost reservation, sandbox, broker, independent verifier and checkpoint review. The owner binds the
   executed task to its slot (``bind_task``); the collector refuses a task that ran on another route or
   another harness version, or whose attempt began before the run started, and refuses to record a run whose
   route configuration changed after the start (the samples would describe two configurations).
3. ``collect`` (identity ``collector``, a TRUSTED_COLLECTOR) derives each sample from the canonical records
   of that task — TaskExecution, WorkerRun, AcceptanceRecord, ReviewRecord, BrokerRequest, the usage
   ledger — never from worker narration. When every slot has a terminal sample the run is recorded through
   ``service.record_result`` (COMPLETED) with gates derived from the same records. ``finish_run`` stops a
   run early: unexecuted slots are recorded as CANCELLED samples (never dropped) and the run is CANCELLED.

Not in this build (``execution_view`` states it): executable fixtures/graders for the held-out case
definitions (each case needs an owner-authored fixture project + trusted suite, ADR-0060) and an automatic
case -> TaskSpec compiler. Nothing here runs a model; a run with no executed tasks stays COLLECTING.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.research import EvalRun, EvalSuite

COLLECTOR = "collector"
TERMINAL_TASK_STATES = ("DONE", "FAILED", "CANCELLED")
FIXTURES_NOTE = ("Fixture/grader eksekusi untuk kasus held-out belum tersedia di build ini: setiap kasus perlu proyek "
                 "fixture + trusted suite yang disusun pemilik (ADR-0060). Kolektor hanya merekam task yang benar-benar "
                 "dijalankan workforce dan diverifikasi.")


def execution_view(run: EvalRun) -> dict[str, Any]:
    bound = len(run.bindings or [])
    if run.status == "PLANNED":
        return {"status": "NOT_STARTED", "bound_slots": 0, "fixtures": "NOT_AVAILABLE_IN_BUILD",
                "note": "Belum dimulai. Eksekusi melalui workforce scheduler (admission, budget, sandbox, verifier); "
                        "planning tidak menjalankan model dan tidak mengubah route. " + FIXTURES_NOTE,
                "remedy": "aeos eval start <run_id>, jalankan task per slot lewat rencana yang disetujui, "
                          "aeos eval bind <run_id> <slot> <task_id>, lalu aeos eval collect <run_id>"}
    if run.status == "RUNNING":
        return {"status": "COLLECTING", "bound_slots": bound, "fixtures": "NOT_AVAILABLE_IN_BUILD",
                "invalid_slots": sum(1 for b in run.bindings or [] if b.get("invalid")),
                "note": "Run berjalan: sampel hanya dari task yang dieksekusi dan diverifikasi. " + FIXTURES_NOTE}
    return {"status": "RECORDED", "bound_slots": bound, "result_source": run.result_source,
            "note": "Hasil tercatat oleh kolektor tepercaya; tidak ditimpa."}


def get_run(session: Session, run_id: str, *, viewer_domain: str | None, scoped: bool) -> EvalRun:
    run = session.get(EvalRun, run_id)
    if run is None:
        raise NotFound("eval run not found")
    if scoped and run.submitted_domain is not None and run.submitted_domain != viewer_domain:
        raise NotFound("eval run not found")  # never reveal a run of the other domain
    return run


def slots(session: Session, run: EvalRun) -> list[dict[str, Any]]:
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == run.suite_key))
    cases = (suite.cases or []) if suite else []
    return [{"slot_id": f"{c['case_id']}#{i}", "case_id": c["case_id"], "kind": c.get("kind"), "repetition": i}
            for c in cases for i in range(1, (run.repetitions or 1) + 1)]


def start_run(session: Session, run_id: str, *, actor_user_id: str, viewer_domain: str | None = None,
              scoped: bool = False) -> EvalRun:
    from aeos.evaluation.service import suite_integrity_ok

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "PLANNED":
        raise Conflict(f"eval run is {run.status}; only a PLANNED run can start")
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == run.suite_key))
    if not suite_integrity_ok(suite):
        raise Conflict("suite missing or its protected content changed: review it before evaluating",
                       code="SUITE_INTEGRITY")
    if not run.route_key:
        raise Conflict("an evaluation executes on a registered route (provider_route UNKNOWN)", code="ROUTE_UNKNOWN")
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key))
    if route is None:
        raise Conflict(f"route {run.route_key} is not registered", code="ROUTE_NOT_REGISTERED")
    if compute_config_hash(route) != run.route_config_hash:
        raise Conflict("the route configuration changed since the run was planned: plan a new run",
                       code="ROUTE_CONFIG_CHANGED")
    run.status = "RUNNING"
    run.started_at = utcnow()
    run.bindings = []
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.start", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, details={"slots": len(slots(session, run))})
    events.emit(session, source="evaluation", kind="eval.run.started", entity_type="eval_run", entity_id=run.id,
                domain=run.submitted_domain, payload={"slots": len(slots(session, run))})
    return run


def route_changed_since_start(session: Session, run: EvalRun) -> str | None:
    """Why the registered route no longer has the configuration the run started on (None = unchanged). A change
    reverted to the same hash during the run still counts (``config_changed_at`` after the start)."""
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == run.route_key)) if run.route_key else None
    if route is None:
        return f"route {run.route_key} is not registered"
    if compute_config_hash(route) != run.route_config_hash:
        return "the route configuration changed since the run started"
    changed_at, began = ensure_aware(route.config_changed_at), ensure_aware(run.started_at)
    if changed_at is not None and began is not None and changed_at > began:  # type: ignore[operator]
        return "the route configuration changed during the run"
    return None


def _task_bound_elsewhere(session: Session, task_id: str, run_id: str) -> str | None:
    for other in session.scalars(select(EvalRun).where(EvalRun.status.in_(("RUNNING", "COMPLETED", "FAILED",
                                                                           "CANCELLED")))):
        if other.id == run_id:
            continue
        if any(b.get("task_id") == task_id for b in other.bindings or []):
            return other.id
    return None


def bind_task(session: Session, run_id: str, *, slot_id: str, task_id: str, actor_user_id: str,
              viewer_domain: str | None = None, scoped: bool = False) -> EvalRun:
    """Bind an executed (or to-be-executed) TaskSpec of an approved plan to one case slot."""
    from aeos.models.planning import TaskSpec

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}; start it before binding tasks")
    slot = next((s for s in slots(session, run) if s["slot_id"] == slot_id), None)
    if slot is None:
        raise ValidationFailed(f"unknown slot {slot_id}")
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    expected_domain = run.submitted_domain or "PERSONAL"
    if task.domain != expected_domain:
        raise NotFound("task not found")  # an OFFICE task never feeds a PERSONAL/platform evaluation
    bindings = [dict(b) for b in run.bindings or []]
    current = next((b for b in bindings if b["slot_id"] == slot_id), None)
    if current is not None and not current.get("invalid"):
        raise Conflict(f"slot {slot_id} is already bound to task {current['task_id']}", code="SLOT_BOUND")
    if any(b.get("task_id") == task_id and b["slot_id"] != slot_id for b in bindings):
        raise Conflict("this task already fills another slot of the run", code="TASK_ALREADY_BOUND")
    other = _task_bound_elsewhere(session, task_id, run.id)
    if other:
        raise Conflict(f"this task already fills a slot of eval run {other}", code="TASK_ALREADY_BOUND")
    bindings = [b for b in bindings if b["slot_id"] != slot_id]
    bindings.append({"slot_id": slot_id, "case_id": slot["case_id"], "repetition": slot["repetition"],
                     "task_id": task_id, "bound_by": actor_user_id, "bound_at": utcnow().isoformat(),
                     "blind_id": "b-" + secrets.token_hex(8)})
    run.bindings = bindings
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.bind", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, details={"slot_id": slot_id, "task_id": task_id})
    return run


# --------------------------------------------------------------------------- the trusted collector


def _seconds(a: Any, b: Any) -> float | None:
    a, b = ensure_aware(a), ensure_aware(b)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())  # type: ignore[operator]


def derive_sample(session: Session, run: EvalRun, binding: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    """(PENDING|READY|INVALID, sample, reason) for one bound slot, from canonical records only."""
    from aeos.models.broker_ext import BrokerRequest
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.evidence import AcceptanceRecord, ReviewRecord
    from aeos.models.workforce import TaskExecution, WorkerRun

    task_id = binding["task_id"]
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))
    if te is None:
        return "PENDING", None, "task not executed yet (no TaskExecution)"
    if te.state not in TERMINAL_TASK_STATES:
        return "PENDING", None, f"task {te.state}"
    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.task_id == task_id)
                                .order_by(WorkerRun.attempt, WorkerRun.created_at)))
    impl = [r for r in runs if (r.run_type or "IMPLEMENTATION") == "IMPLEMENTATION"]
    used = {r.model_route_key for r in impl if r.model_route_key}
    if run.route_key and used - {run.route_key}:
        return "INVALID", None, f"task ran on other routes {sorted(used - {run.route_key})}: not this configuration"
    began = ensure_aware(run.started_at)
    early = [r for r in impl if began is not None and r.created_at is not None
             and ensure_aware(r.created_at) < began]  # type: ignore[operator]
    if early:
        # start_run proved the route configuration; an attempt made before it may have run on another one
        return "INVALID", None, "task attempt began before this evaluation started: configuration not proven"
    harness = (run.configuration or {}).get("harness_version")
    engines = {r.engine_version for r in impl if r.engine_version}
    if harness not in (None, "UNKNOWN") and engines - {harness}:
        return "INVALID", None, f"task ran on harness {sorted(engines)} but the configuration says {harness}"
    if te.state == "DONE" and not impl:
        return "INVALID", None, "task DONE without a recorded worker run"
    acc = session.scalar(select(AcceptanceRecord).where(AcceptanceRecord.task_id == task_id)
                         .order_by(AcceptanceRecord.updated_at.desc(), AcceptanceRecord.created_at.desc()))
    rate_limited = any(r.quota_status == "RATE_LIMITED" for r in impl)
    last = impl[-1] if impl else None
    if te.state == "DONE":
        if acc is None or acc.acceptance_status != "ACCEPTED":
            return "INVALID", None, "task DONE without a valid ACCEPTED acceptance record"
        status = "ACCEPTED"
    elif rate_limited:
        status = "QUOTA_INTERRUPTED"
    elif te.state == "CANCELLED":
        status = "CANCELLED"
    elif last is not None and last.outcome_code == "TIMEOUT":
        status = "TIMEOUT"
    elif (acc is not None and acc.acceptance_status == "REJECTED") or any(
            r.verifier_status == "FAIL" or r.acceptance_status == "REJECTED" for r in impl):
        status = "REJECTED"
    else:
        status = "FAILED"
    run_ids = [r.id for r in impl]
    requests = list(session.scalars(select(BrokerRequest).where(BrokerRequest.run_id.in_(run_ids)))) if run_ids else []
    ledger = list(session.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.run_id.in_(run_ids)))) if run_ids else []
    worker_ledger = [e for e in ledger if e.category == "WORKER_API"]
    ci_ledger = [e for e in ledger if e.category in ("CI", "COMPUTE")]
    reviews = list(session.scalars(select(ReviewRecord).where(ReviewRecord.task_id == task_id)))
    reasons = [str(x) for x in (acc.reasons or [])] if acc is not None else []
    tokens = [(r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in requests
              if r.prompt_tokens is not None or r.completion_tokens is not None]
    first_started = next((r.started_at for r in impl if r.started_at), None)
    finished = next((r.finished_at for r in reversed(impl) if r.finished_at), None)
    sample: dict[str, Any] = {
        "sample_id": binding["slot_id"], "case_id": binding["case_id"], "repetition": binding["repetition"],
        "task_id": task_id, "status": status, "collector": COLLECTOR,
        "first_pass": status == "ACCEPTED" and len(impl) == 1 and not (te.extra_attempts or 0),
        "repair_count": max(0, len(impl) - 1),
        "latency_s": _seconds(first_started, finished),
        "queue_wait_s": _seconds(te.created_at, first_started),
        "worker_cost_usd": (None if not worker_ledger or any(e.cost_usd is None for e in worker_ledger)
                            else float(sum(float(e.cost_usd or 0) for e in worker_ledger))),
        "compute_ci_cost_usd": (0.0 if not ci_ledger else None if any(e.cost_usd is None for e in ci_ledger)
                                else float(sum(float(e.cost_usd or 0) for e in ci_ledger))),
        "worker_tokens": sum(tokens) if tokens else None,
        "permission_attempts": sum(1 for r in requests if r.outcome in ("DENIED", "CAP_EXCEEDED")),
        "provider_unavailable": any(r.outcome in ("UPSTREAM_ERROR", "TIMEOUT") for r in requests),
        "scope_violations": sum(len(r.quarantine_reasons or []) for r in impl)
        + sum(1 for r in impl if r.outcome_code == "SCOPE_VIOLATION"),
        "security_violations": sum(1 for r in impl if r.quarantined)
        + sum(1 for x in reasons if "security review" in x.lower()),
        "critical_regressions": sum(1 for x in reasons if "non-regression" in x.lower()),
        "claude_review_count": sum(1 for r in reviews if r.reviewer_kind == "CLAUDE"),
        "human_interventions": sum(1 for r in reviews if r.reviewer_kind == "HUMAN") + int(bool(te.extra_attempts)),
        "quota_delay_s": None,
        "unknown_fields": ["false_claims", "tool_failures", "context_tokens", "coordination_tokens", "merge_conflicts"],
    }
    if binding.get("quality"):
        sample["quality"] = dict(binding["quality"])
    return "READY", sample, ""


def derive_gates(samples: list[dict[str, Any]]) -> dict[str, str]:
    """Security/authority/evidence gates from the collected records (never from a worker claim)."""
    observed = [s for s in samples if s.get("collector") == COLLECTOR]
    if not observed:
        return {"security": "NOT_RUN", "authority": "NOT_RUN", "evidence": "NOT_RUN"}
    security = "FAIL" if any(s.get("security_violations") for s in observed) else "PASS"
    authority = "FAIL" if any(s.get("permission_attempts") or s.get("scope_violations") for s in observed) else "PASS"
    # every ACCEPTED sample rests on an ACCEPTED AcceptanceRecord by construction (derive_sample)
    evidence = "PASS"
    return {"security": security, "authority": authority, "evidence": evidence}


def collect(session: Session, run_id: str, *, viewer_domain: str | None = None, scoped: bool = False) -> dict[str, Any]:
    """Collect every bound slot; record the run when all slots have a terminal, valid sample."""
    from aeos.evaluation.service import record_result

    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}; only a RUNNING run is collected")
    changed = route_changed_since_start(session, run)
    if changed:
        raise Conflict(f"{changed}: the samples would mix configurations; stop it (aeos eval finish) and plan a new "
                       "run on the current configuration", code="ROUTE_CONFIG_CHANGED")
    all_slots = slots(session, run)
    by_slot = {b["slot_id"]: b for b in run.bindings or []}
    collected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unbound = [s["slot_id"] for s in all_slots if s["slot_id"] not in by_slot]
    bindings = [dict(b) for b in run.bindings or []]
    for b in bindings:
        state, sample, why = derive_sample(session, run, b)
        b.pop("invalid", None)
        if state == "READY":
            collected.append(sample)  # type: ignore[arg-type]
        elif state == "INVALID":
            b["invalid"] = why
            invalid.append({"slot_id": b["slot_id"], "task_id": b["task_id"], "reason": why})
        else:
            pending.append({"slot_id": b["slot_id"], "task_id": b["task_id"], "reason": why})
    run.bindings = bindings
    session.flush()
    out = {"run_id": run.id, "slots": len(all_slots), "collected": len(collected), "pending": pending,
           "invalid": invalid, "unbound": unbound, "recorded": False}
    if all_slots and not pending and not invalid and not unbound:
        record_result(session, run.id, samples=collected, gate_results=derive_gates(collected), result_source=COLLECTOR)
        out["recorded"] = True
        out["status"] = run.status
    return out


def finish_run(session: Session, run_id: str, *, actor_user_id: str, reason: str, viewer_domain: str | None = None,
               scoped: bool = False) -> EvalRun:
    """Stop a RUNNING run early: collected samples are kept, every other slot is recorded as CANCELLED
    (never dropped) and the run ends CANCELLED (not ranked)."""
    from aeos.evaluation.service import record_result

    if len((reason or "").strip()) < 5:
        raise ValidationFailed("reason is required")
    run = get_run(session, run_id, viewer_domain=viewer_domain, scoped=scoped)
    if run.status != "RUNNING":
        raise Conflict(f"eval run is {run.status}")
    by_slot = {b["slot_id"]: b for b in run.bindings or []}
    samples: list[dict[str, Any]] = []
    for s in slots(session, run):
        b = by_slot.get(s["slot_id"])
        state, sample, why = derive_sample(session, run, b) if b else ("PENDING", None, "slot never bound")
        if state == "READY":
            samples.append(sample)  # type: ignore[arg-type]
        else:
            samples.append({"sample_id": s["slot_id"], "case_id": s["case_id"], "repetition": s["repetition"],
                            "status": "CANCELLED", "cancel_reason": why or reason, "collector": COLLECTOR})
    record_result(session, run.id, samples=samples, gate_results=derive_gates(samples), result_source=COLLECTOR,
                  status="CANCELLED")
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="eval.run.finish", target_type="eval_run",
                 target_id=run.id, domain=run.submitted_domain, reason=reason[:500])
    return run
