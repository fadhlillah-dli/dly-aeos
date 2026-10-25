"""Deterministic scheduler (§14, §19, §21, §22): readiness, admission, atomic leases, heartbeats,
reconciliation, cancel/repair/accept. No LLM is involved anywhere in this module.

Services take a Session and never commit (callers commit). Every state change is audited and
emitted as an event (task.state_changed / run.state_changed / run.heartbeat / run.action).
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import TERMINAL_EXECUTION_STATES, Capability, DataMode, ExecutionState, Verdict
from aeos.core.errors import AeosError, Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.ids import new_id
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.identity import SystemSetting
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.routing import ModelRoute
from aeos.models.workforce import AgentDefinition, Lease, TaskExecution, WorkerRun
from aeos.workforce import integrations as ext
from aeos.workforce.catalog import IMPLEMENTER_AGENT_KEY, REVIEWER_AGENT_KEYS
from aeos.workforce.states import ACTIVE_STATES, RUN_TERMINAL, RUNNING_STOP_CODES, is_legal

log = logging.getLogger("aeos.workforce.scheduler")
S = ExecutionState

CONTROLLER_ID = f"{socket.gethostname()}:{os.getpid()}:{new_id()[:8]}"
KEY_PAUSED = "scheduler.paused"
KEY_ENABLED = "scheduler.enabled"
KEY_CONCURRENCY = "workforce.concurrency"
KEY_LEASE_TTL = "workforce.lease_ttl_seconds"
DEFAULT_LEASE_TTL = 120
DEFAULT_MAX_ATTEMPTS = 2
HARD_MAX_ACTIVE = 2  # §19/§21 pilot: 1 by default, 2 only with a recorded decision
DEFAULT_MAX_REQUESTS = 300
DEFAULT_MAX_TOKENS = 2_000_000

# Reason codes that keep a task waiting in READY (re-evaluated every tick, no state churn).
WAIT_CODES = frozenset({"SCHEDULING_PAUSED", "CONCURRENCY_CAP", "SHARED_WRITE_CONFLICT", "PLAN_NOT_APPROVED",
                        "DEPENDENCY_NOT_ACCEPTED", "AGENT_DISABLED", "OFFICE_DENIED", "CAPABILITY_NOT_READY",
                        "SANDBOX_NOT_READY", "ROUTE_INELIGIBLE", "COST_UNKNOWN", "BUDGET_EXCEEDED",
                        "MODULE_UNAVAILABLE", "VERIFIER_UNAVAILABLE", "SIDE_EFFECT_UNRECONCILED",
                        "LEASE_LOST_PROCESS_ALIVE", "QUOTA_EXHAUSTED"})
QUOTA_CODES = frozenset({"BUDGET_EXCEEDED", "QUOTA_EXHAUSTED"})
# BLOCKED reason codes that only an owner action clears (retry_task / quarantine release), never a tick.
HUMAN_BLOCK_CODES = frozenset({"QUARANTINED", "REVIEW_BLOCKED", "WORKER_BLOCKER"})


# ----------------------------------------------------------------------------- small helpers
def _actor_type(actor: str) -> str:
    head = actor.split(":", 1)[0]
    return head if head in {"user", "system", "worker", "service", "cli"} else "system"


def _audit(session: Session, actor: str, action: str, *, target_type: str, target_id: str, project_id: str | None,
           domain: str | None, result: str = "SUCCESS", reason: str = "", details: dict[str, Any] | None = None) -> None:
    audit.record(session, actor_type=_actor_type(actor), actor_id=actor, action=action, result=result,
                 target_type=target_type, target_id=target_id, project_id=project_id, domain=domain,
                 reason=reason, details=details or {})


def _setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(SystemSetting, key)
    return default if row is None else row.value


def _set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    session.flush()


def get_te(session: Session, task_id: str, *, fresh: bool = True) -> TaskExecution:
    q = select(TaskExecution).where(TaskExecution.task_id == task_id)
    if fresh:
        q = q.execution_options(populate_existing=True)
    te = session.scalar(q)
    if te is None:
        raise NotFound("task execution not found")
    return te


def get_run(session: Session, run_id: str) -> WorkerRun:
    run = session.get(WorkerRun, run_id, populate_existing=True)
    if run is None:
        raise NotFound("run not found")
    return run


def max_attempts(task: TaskSpec, te: TaskExecution | None = None) -> int:
    try:
        base = int((task.limits or {}).get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        base = DEFAULT_MAX_ATTEMPTS
    return max(1, base) + (te.extra_attempts if te else 0)


def lease_ttl(session: Session) -> int:
    try:
        return max(30, min(int(_setting(session, KEY_LEASE_TTL, DEFAULT_LEASE_TTL)), 3600))
    except (TypeError, ValueError):
        return DEFAULT_LEASE_TTL


# ----------------------------------------------------------------------------- scheduling settings
def scheduling_paused(session: Session) -> bool:
    v = _setting(session, KEY_PAUSED)
    return bool(v.get("paused")) if isinstance(v, dict) else bool(v)


def pause_info(session: Session) -> dict[str, Any]:
    v = _setting(session, KEY_PAUSED)
    return v if isinstance(v, dict) else {"paused": bool(v)}


def set_scheduling_paused(session: Session, paused: bool, *, actor: str, reason: str = "") -> None:
    _set_setting(session, KEY_PAUSED, {"paused": bool(paused), "reason": redact_text(reason)[:300], "by": actor,
                                       "at": utcnow().isoformat()})
    _audit(session, actor, "workforce.scheduling.pause" if paused else "workforce.scheduling.resume",
           target_type="scheduler", target_id="scheduler", project_id=None, domain=None, reason=reason)
    events.emit(session, source="controller", kind="scheduler.paused" if paused else "scheduler.resumed",
                entity_type="scheduler", entity_id="scheduler", payload={"paused": bool(paused), "reason": reason})


def scheduler_enabled(session: Session) -> bool:
    return bool(_setting(session, KEY_ENABLED, False))


def set_scheduler_enabled(session: Session, enabled: bool, *, actor: str) -> None:
    _set_setting(session, KEY_ENABLED, bool(enabled))
    _audit(session, actor, "workforce.scheduler.enable" if enabled else "workforce.scheduler.disable",
           target_type="scheduler", target_id="scheduler", project_id=None, domain=None)
    events.emit(session, source="controller", kind="scheduler.enabled_changed", entity_type="scheduler",
                entity_id="scheduler", payload={"enabled": bool(enabled)})


CONCURRENCY_TARGET = ("workforce", "concurrency")  # policy.catalog.TARGET_WORKFORCE / TARGET_ID_CONCURRENCY


def _concurrency_decision(session: Session) -> Decision | None:
    v = _setting(session, KEY_CONCURRENCY)
    if not isinstance(v, dict):
        return None
    try:
        wanted = min(int(v.get("max_active", 1)), HARD_MAX_ACTIVE)
    except (TypeError, ValueError):
        return None
    if wanted <= 1:
        return None
    d = session.get(Decision, str(v.get("decision_id") or ""))
    # §19: only a human POLICY decision on exactly workforce/concurrency counts (never another decision that
    # happens to carry max_active_workers in its details).
    if (d is None or d.status != "ACTIVE" or d.actor_kind != "HUMAN" or d.kind != "POLICY"
            or (d.target_type, d.target_id) != CONCURRENCY_TARGET):
        return None
    exp = ensure_aware(d.expires_at)
    if exp is not None and exp <= utcnow():
        return None
    return d


def _scope_matches(scope: dict[str, Any], task: TaskSpec | None) -> bool:
    """§44 a mode is adopted per task class, not for every project/role at once."""
    if task is None:
        return True
    for key, value in (("project_ids", task.project_id), ("role_keys", task.role_key),
                       ("risk_classes", task.risk_class), ("domains", task.domain)):
        allowed = scope.get(key)
        if isinstance(allowed, list) and allowed and value not in allowed:
            return False
    return True


def concurrency_cap(session: Session, task: TaskSpec | None = None) -> tuple[int, str | None]:
    """1 active implementation worker; 2 only with an ACTIVE human POLICY decision on workforce/concurrency
    whose task-class scope covers *task* (§19, §21, §44)."""
    v = _setting(session, KEY_CONCURRENCY)
    d = _concurrency_decision(session)
    if d is None or not isinstance(v, dict):
        return 1, None
    details = d.details or {}
    if not _scope_matches(details.get("scope") or {}, task):
        return 1, None
    try:
        allowed = int(details.get("max_active_workers", 1))
    except (TypeError, ValueError):
        allowed = 1
    wanted = min(int(v.get("max_active", 1)), HARD_MAX_ACTIVE)
    return (min(wanted, allowed), d.id) if allowed >= 2 else (1, None)


BASELINE_TOPOLOGY = "A"  # §44 sequential baseline
PARALLEL_TOPOLOGY = "C"  # §44 bounded parallel (two active workers)


def _metric(run: Any, dim: str, key: str) -> float | None:
    v = ((run.metrics or {}).get(dim) or {}).get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def topology_benefit(candidate: Any, baseline: Any) -> dict[str, Any]:
    """§44 "Mode baru dipromosikan hanya untuk kelas tugas yang terbukti bermanfaat vs baseline": the bounded-parallel
    run (topology C) is compared with the sequential baseline (topology A) on the SAME suite, the SAME fixed
    model/tool configuration and the SAME conditions. It must be no worse on outcome, safety and contract and
    strictly better on accepted outcomes or latency/queue wait. Missing metrics are UNKNOWN, never a benefit."""
    problems: list[str] = []
    if baseline is None:
        return {"status": "NO_BASELINE", "problems": ["no sequential baseline evaluation run (topology A) given"]}
    if candidate.topology != PARALLEL_TOPOLOGY:
        problems.append(f"the evaluation run must test topology {PARALLEL_TOPOLOGY} (bounded parallel), "
                        f"not {candidate.topology or 'none'}")
    if baseline.topology != BASELINE_TOPOLOGY:
        problems.append(f"the baseline run must be topology {BASELINE_TOPOLOGY} (sequential), "
                        f"not {baseline.topology or 'none'}")
    if baseline.status != "COMPLETED":
        problems.append(f"the baseline run is {baseline.status}, not COMPLETED")
    if baseline.suite_key != candidate.suite_key:
        problems.append("baseline and candidate ran different suites")
    if baseline.configuration_hash != candidate.configuration_hash:
        problems.append("baseline and candidate used different model/tool configurations (§44 fixed configuration)")
    if not baseline.conditions_hash or baseline.conditions_hash != candidate.conditions_hash:
        problems.append("baseline and candidate did not run under the same recorded conditions")
    if (baseline.submitted_domain or None) != (candidate.submitted_domain or None):
        problems.append("baseline and candidate were planned in different domains")
    if problems:
        return {"status": "NOT_COMPARABLE", "problems": problems}
    rows = {
        "final_acceptance_rate": ("outcome", "final_acceptance_rate", "higher"),
        "critical_regressions": ("outcome", "critical_regressions", "lower"),
        "security_violations": ("contract", "security_violations", "lower"),
        "scope_adherence_rate": ("contract", "scope_adherence_rate", "higher"),
        "latency_p50_s": ("performance", "latency_p50_s", "lower"),
        "queue_wait_p50_s": ("performance", "queue_wait_p50_s", "lower"),
    }
    compared: dict[str, dict[str, Any]] = {}
    for name, (dim, key, better) in rows.items():
        b, c = _metric(baseline, dim, key), _metric(candidate, dim, key)
        verdict = "UNKNOWN" if b is None or c is None else (
            "BETTER" if (c > b if better == "higher" else c < b) else "EQUAL" if c == b else "WORSE")
        compared[name] = {"baseline": b, "candidate": c, "better_is": better, "verdict": verdict}
    guards = ("final_acceptance_rate", "critical_regressions", "security_violations", "scope_adherence_rate")
    worse = [k for k in guards if compared[k]["verdict"] == "WORSE"]
    unknown = [k for k in guards if compared[k]["verdict"] == "UNKNOWN"]
    gains = [k for k in ("final_acceptance_rate", "latency_p50_s", "queue_wait_p50_s")
             if compared[k]["verdict"] == "BETTER"]
    if worse:
        problems.append(f"worse than the sequential baseline on {', '.join(worse)}")
    if unknown:
        problems.append(f"not measured on both runs: {', '.join(unknown)} (UNKNOWN is not a benefit)")
    if not gains:
        problems.append("no measured benefit over the baseline (accepted outcomes or latency/queue wait)")
    return {"status": "BENEFIT" if not problems else "NO_BENEFIT", "problems": problems, "gains": gains,
            "compared": compared, "baseline_eval_run_id": baseline.id, "eval_run_id": candidate.id}


def set_concurrency(session: Session, *, max_active: int, actor: str, actor_user_id: str, rationale: str,
                    eval_run_id: str | None = None, scope: dict[str, Any] | None = None,
                    decision_record: dict[str, Any] | None = None,
                    baseline_eval_run_id: str | None = None) -> dict[str, Any]:
    """Owner action (§19 "Dua worker aktif adalah langkah eksperimen berikutnya"): 2 workers need a human POLICY
    decision on workforce/concurrency that references a COMPLETED bounded-parallel evaluation run (topology C) with
    all gates PASS (integration evidence) that shows a measured benefit over the sequential baseline run (topology
    A) of the same suite/configuration/conditions (§44), states the task-class scope and the rollback (previous
    mode), and carries the §44 DecisionRecord fields. ``max_active=1`` returns to the pilot mode (no decision
    needed)."""
    if max_active not in (1, 2):
        raise ValidationFailed("max_active must be 1 or 2 (pilot cap)")
    previous, _ = concurrency_cap(session)
    if max_active == 1:
        _set_setting(session, KEY_CONCURRENCY, {"max_active": 1, "decision_id": None, "by": actor,
                                                "at": utcnow().isoformat()})
        _audit(session, actor, "workforce.concurrency.set", target_type="workforce", target_id="concurrency",
               project_id=None, domain=None, reason=rationale, details={"max_active": 1, "previous": previous})
        return {"max_active": 1, "decision_id": None, "previous": previous}
    if not (rationale or "").strip() or len(rationale.strip()) < 10:
        raise ValidationFailed("a rationale (min 10 characters) is required")
    from aeos.models.research import EvalRun
    from aeos.policy import catalog as policy_catalog

    run = session.get(EvalRun, eval_run_id) if eval_run_id else None
    gates = {k: str(v).upper() for k, v in ((run.gate_results or {}) if run else {}).items()}
    if run is None or run.status != "COMPLETED" or not gates or any(v != "PASS" for v in gates.values()):
        raise Blocked("two workers need integration evidence: a COMPLETED evaluation run with every gate PASS",
                      code="CONCURRENCY_EVIDENCE_MISSING",
                      details={"eval_run_id": eval_run_id, "status": run.status if run else None, "gates": gates})
    baseline = session.get(EvalRun, baseline_eval_run_id) if baseline_eval_run_id else None
    benefit = topology_benefit(run, baseline)
    if benefit["status"] != "BENEFIT":
        raise Blocked("two workers are adopted only for a task class where the bounded-parallel topology (C) is "
                      "proven better than the sequential baseline (A) on the same configuration: "
                      + "; ".join(benefit["problems"]), code="CONCURRENCY_BENEFIT_NOT_PROVEN", details=benefit)
    record = dict(decision_record or {})
    record.setdefault("evidence_ids", [run.id, baseline.id] if baseline is not None else [run.id])
    missing = [k for k in policy_catalog.DECISION_RECORD_FIELDS if not record.get(k)]
    if missing:
        raise ValidationFailed(f"decision record incomplete: {missing}", code="DECISION_RECORD_INCOMPLETE",
                               details={"required": list(policy_catalog.DECISION_RECORD_FIELDS)})
    clean_scope = {k: [str(x) for x in v] for k, v in (scope or {}).items()
                   if k in ("project_ids", "role_keys", "risk_classes", "domains") and isinstance(v, list) and v}
    if not clean_scope:
        # §44 "tidak otomatis diperluas ke semua proyek/peran": the mode is adopted for the task class it was proven
        # on, never for every project/role at once
        raise ValidationFailed("two workers need a task-class scope (project_ids, role_keys, risk_classes or domains)",
                               code="CONCURRENCY_SCOPE_REQUIRED")
    d = ext.record_decision(
        session, kind="POLICY", actor_user_id=actor_user_id, target_type=policy_catalog.TARGET_WORKFORCE,
        target_id=policy_catalog.TARGET_ID_CONCURRENCY, target_hash=content_hash({"max_active": 2, "scope": clean_scope}),
        allowed_operations=["run_two_workers"], auth_scope={"scope": clean_scope or "all task classes"},
        rationale=rationale,
        details={"max_active_workers": 2, "eval_run_id": run.id, "suite_key": run.suite_key, "scope": clean_scope,
                 "baseline_eval_run_id": benefit["baseline_eval_run_id"],
                 "benefit": {"gains": benefit["gains"], "compared": benefit["compared"]},
                 "rollback": {"previous_max_active": previous, "how": "set concurrency 1 (no decision needed)"},
                 "decision_record": record})
    _set_setting(session, KEY_CONCURRENCY, {"max_active": 2, "decision_id": d.id, "by": actor,
                                            "at": utcnow().isoformat()})
    _audit(session, actor, "workforce.concurrency.set", target_type="workforce", target_id="concurrency",
           project_id=None, domain=None, reason=rationale, details={"max_active": 2, "decision_id": d.id})
    events.emit(session, source="controller", kind="scheduler.concurrency_changed", entity_type="scheduler",
                entity_id="scheduler", payload={"max_active": 2, "decision_id": d.id, "scope": clean_scope})
    return {"max_active": 2, "decision_id": d.id, "previous": previous, "scope": clean_scope}


# ----------------------------------------------------------------------------- transitions
def _emit_task(session: Session, te: TaskExecution, src: str, dst: str, reason: str, actor: str) -> None:
    _audit(session, actor, "workforce.task.transition", target_type="task", target_id=te.task_id,
           project_id=te.project_id, domain=te.domain, reason=reason,
           details={"from": src, "to": dst, "reason_code": te.reason_code, "row_version": te.row_version})
    events.emit(session, source="scheduler", kind="task.state_changed", entity_type="task", entity_id=te.task_id,
                entity_version=str(te.row_version), project_id=te.project_id, domain=te.domain,
                run_id=te.active_run_id, mode=te.mode,
                payload={"from": src, "to": dst, "reason": redact_text(reason)[:500], "reason_code": te.reason_code,
                         "task_key": te.task_key})


def _emit_run(session: Session, run: WorkerRun, src: str, dst: str, reason: str, actor: str) -> None:
    _audit(session, actor, "workforce.run.transition", target_type="run", target_id=run.id,
           project_id=run.project_id, domain=run.domain, reason=reason, details={"from": src, "to": dst})
    events.emit(session, source="scheduler", kind="run.state_changed", entity_type="run", entity_id=run.id,
                project_id=run.project_id, domain=run.domain, run_id=run.id, attempt=run.attempt,
                lease_id=run.lease_id, mode=run.mode,
                payload={"from": src, "to": dst, "reason": redact_text(reason)[:500], "task_id": run.task_id})


def set_run_status(session: Session, run: WorkerRun, status: str, *, reason: str, actor: str,
                   outcome_code: str | None = None) -> None:
    src = run.execution_status
    if src == status:
        return
    if src in RUN_TERMINAL:
        raise Conflict(f"run {run.id} is already {src}")
    run.execution_status = status
    if outcome_code is not None:
        run.outcome_code = outcome_code
    if status in RUN_TERMINAL or status == S.REVIEW.value:
        run.finished_at = run.finished_at or utcnow()
    if reason and status in (S.FAILED.value, S.CANCELLED.value, S.BLOCKED.value):
        run.block_reason = redact_text(reason)[:2000]
    session.flush()
    _emit_run(session, run, src, status, reason, actor)


def transition_task(
    session: Session, task_id: str, to_state: str, *, reason: str, actor: str, expected_from: str | None = None,
    reason_code: str = "", _allow_done: bool = False, _allow_reopen: bool = False, **set_fields: Any,
) -> TaskExecution:
    """Apply a legal §14 transition with optimistic concurrency; raise ``Conflict`` otherwise."""
    te = get_te(session, task_id)
    src = te.state
    try:
        dst = S(to_state).value
    except ValueError as exc:
        raise ValidationFailed(f"unknown execution state {to_state!r}") from exc
    if dst == S.DONE.value and not _allow_done:
        raise Conflict("DONE is reachable only through mark_accepted with a valid AcceptanceRecord",
                       code="DONE_REQUIRES_ACCEPTANCE")
    if src == S.DONE.value and not _allow_reopen:
        raise Conflict("a DONE task is reopened only through reopen_for_reverification when its acceptance "
                       "became STALE", code="REOPEN_REQUIRES_STALE_ACCEPTANCE")
    if expected_from is not None and src != expected_from:
        raise Conflict(f"task is {src}, expected {expected_from}", code="STATE_CHANGED")
    if not is_legal(src, dst, reason_code=reason_code):
        _audit(session, actor, "workforce.task.transition", result="DENIED", target_type="task", target_id=task_id,
               project_id=te.project_id, domain=te.domain, reason=f"illegal {src}->{dst}",
               details={"reason_code": reason_code})
        raise Conflict(f"illegal transition {src} -> {dst}", code="ILLEGAL_TRANSITION",
                       details={"from": src, "to": dst})
    values: dict[str, Any] = {
        "state": dst, "reason": redact_text(reason)[:2000], "reason_code": reason_code,
        "row_version": te.row_version + 1, "state_changed_at": utcnow(), "updated_at": utcnow(),
    }
    values.update(set_fields)
    res = session.execute(update(TaskExecution).where(TaskExecution.id == te.id, TaskExecution.state == src,
                                                      TaskExecution.row_version == te.row_version).values(**values))
    if res.rowcount != 1:
        raise Conflict("task execution changed concurrently", code="CONCURRENT_UPDATE")
    te = get_te(session, task_id)
    _emit_task(session, te, src, dst, reason, actor)
    return te


def _set_wait_reason(session: Session, te: TaskExecution, code: str, reason: str) -> None:
    """Annotate a READY task with why it waits — not a state change, so no row_version bump."""
    reason = redact_text(reason)[:2000]
    if te.reason_code == code and te.reason == reason:
        return
    te.reason_code = code
    te.reason = reason
    session.flush()
    events.emit(session, source="scheduler", kind="task.wait_reason", entity_type="task", entity_id=te.task_id,
                project_id=te.project_id, domain=te.domain, mode=te.mode,
                payload={"state": te.state, "reason_code": code, "reason": reason, "task_key": te.task_key})


# ----------------------------------------------------------------------------- observed run fields
_RUN_FIELD_VOCAB: dict[str, set[str] | None] = {
    # The verifier's own vocabulary (ADR-0060: PASS/FAIL/QUARANTINED/NOT_PROVEN/BLOCKED) is the
    # contract; PASSED/FAILED/ERROR stay accepted for older callers (integration amendment, ADR-0050).
    "verifier_status": {"NOT_RUN", "RUNNING", "PASS", "FAIL", "QUARANTINED", "PASSED", "FAILED", "ERROR",
                        "NOT_PROVEN", "BLOCKED"},
    "review_verdict": {*(v.value for v in Verdict), "PENDING"},
    "acceptance_status": {"PENDING", "ACCEPTED", "REJECTED", "STALE", "INVALIDATED"},
    "merged_status": {"NOT_MERGED", "PR_OPEN", "MERGED", "RELEASED", "UNKNOWN"},
    "worker_reported_status": None,
    "quota_status": None,
}
# ``head_commit`` is deliberately NOT updatable here: it is recorded by the runtime commit and is the
# binding for publication and quarantine-release decisions (ADR-0054).
_RUN_FREE_FIELDS = {"current_action", "touched_paths", "review_record_id", "acceptance_id", "evidence_refs",
                    "model_resolved", "engine_version", "usage_summary"}


def update_run(session: Session, run_id: str, *, actor: str, **fields: Any) -> WorkerRun:
    """Update observed run fields. Each status stays a separate field (§35); none of them moves
    the canonical execution state — DONE needs mark_accepted."""
    run = get_run(session, run_id)
    unknown = set(fields) - set(_RUN_FIELD_VOCAB) - _RUN_FREE_FIELDS
    if unknown:
        raise ValidationFailed(f"fields not updatable via update_run: {sorted(unknown)}")
    changes: dict[str, Any] = {}
    for k, v in fields.items():
        if k in _RUN_FIELD_VOCAB:
            if v is None:
                setattr(run, k, None)
                changes[k] = None
                continue
            val = str(v).upper()[:32]
            vocab = _RUN_FIELD_VOCAB[k]
            if vocab is not None and val not in vocab:
                raise ValidationFailed(f"{k} must be one of {sorted(vocab)}")
            setattr(run, k, val)
            changes[k] = val
        elif k == "touched_paths":
            merged = list(dict.fromkeys([*(run.touched_paths or []), *[str(p)[:400] for p in (v or [])]]))
            run.touched_paths = merged[:2000]
            changes[k] = len(merged)
        elif k == "evidence_refs":
            run.evidence_refs = [*(run.evidence_refs or []), *list(v or [])][:500]
            changes[k] = len(run.evidence_refs)
        elif k == "usage_summary":
            run.usage_summary = redact(dict(v or {}))
            changes[k] = "updated"
        else:
            sv = redact_text(str(v)) if v is not None else None
            setattr(run, k, sv[:4000] if isinstance(sv, str) else sv)
            changes[k] = (sv or "")[:200] if isinstance(sv, str) else sv
    session.flush()
    _audit(session, actor, "workforce.run.update", target_type="run", target_id=run.id, project_id=run.project_id,
           domain=run.domain, details=changes)
    events.emit(session, source="scheduler", kind="run.updated", entity_type="run", entity_id=run.id,
                run_id=run.id, attempt=run.attempt, project_id=run.project_id, domain=run.domain, mode=run.mode,
                payload=changes)
    return run


# ----------------------------------------------------------------------------- executions & readiness
def ensure_executions(session: Session, plan_version_id: str, *, actor: str, requested_by: str | None = None,
                      mode: str = DataMode.LIVE.value) -> list[TaskExecution]:
    version = session.get(PlanVersion, plan_version_id)
    if version is None:
        raise NotFound("plan version not found")
    tasks = list(session.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == plan_version_id)))
    out = []
    for t in tasks:
        te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == t.id))
        if te is None:
            # A MOCK/DEMO TaskSpec (fixture plan content) never becomes LIVE work: its execution keeps
            # the fixture label, so the LIVE scheduler never admits it (§35/§50, ADR-0050 amendment).
            te_mode = mode if mode != DataMode.LIVE.value else (task_data_mode(t, version) or mode)
            te = TaskExecution(task_id=t.id, project_id=t.project_id, domain=t.domain, state=S.QUEUED.value,
                               plan_id=version.plan_id, plan_version_id=plan_version_id, task_key=t.task_key,
                               requested_by=requested_by, mode=te_mode, state_changed_at=utcnow(),
                               assigned_agent_key=IMPLEMENTER_AGENT_KEY)
            session.add(te)
            session.flush()
            _audit(session, actor, "workforce.task.enqueue", target_type="task", target_id=t.id,
                   project_id=t.project_id, domain=t.domain, details={"plan_version_id": plan_version_id})
            events.emit(session, source="scheduler", kind="task.state_changed", entity_type="task", entity_id=t.id,
                        entity_version="1", project_id=t.project_id, domain=t.domain, mode=te.mode,
                        payload={"from": None, "to": S.QUEUED.value, "task_key": t.task_key})
        out.append(te)
    return out


NON_LIVE_MODES = frozenset({DataMode.MOCK.value, DataMode.DEMO.value})


def task_data_mode(task: TaskSpec, version: PlanVersion | None = None) -> str | None:
    """MOCK/DEMO when the TaskSpec (or its PlanVersion) is fixture content, else None (LIVE)."""
    for m in (getattr(task, "mode", None), getattr(version, "mode", None)):
        if m in NON_LIVE_MODES:
            return str(m)
    return None


def validate_dag(tasks: list[TaskSpec]) -> dict[str, str]:
    """§14: unknown nodes, self-dependency and cycles are rejected (task_key -> problem)."""
    keys = {t.task_key for t in tasks}
    deps = {t.task_key: list(t.depends_on or []) for t in tasks}
    problems: dict[str, str] = {}
    for k, ds in deps.items():
        if k in ds:
            problems[k] = "self-dependency"
        unknown = [d for d in ds if d not in keys]
        if unknown:
            problems[k] = f"unknown dependency {unknown}"
    color: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> None:
        color[n] = 1
        for d in deps.get(n, []):
            if d not in deps:
                continue
            if color.get(d) == 1:
                for m in stack[stack.index(d):] if d in stack else [n, d]:
                    problems.setdefault(m, "dependency cycle")
                problems.setdefault(n, "dependency cycle")
            elif color.get(d) is None:
                visit(d, [*stack, d])
        color[n] = 2

    for k in deps:
        if color.get(k) is None:
            visit(k, [k])
    return problems


def _previous_worker_alive(session: Session, task_id: str) -> WorkerRun | None:
    from aeos.worker_runtime import processes

    for r in session.scalars(select(WorkerRun).where(WorkerRun.task_id == task_id, WorkerRun.pid.is_not(None))):
        if processes.is_alive(r.pid, r.pid_create_time):
            return r
    return None


def _unreconciled_side_effects(session: Session, task_id: str) -> list[tuple[str, str]]:
    """§14 "Crash recovery membaca lease, reservation, publication records, dan status eksternal
    sebelum retry": an earlier attempt's push/PR whose outcome is PENDING/UNKNOWN blocks a retry."""
    from aeos.models.broker_ext import BrokerSideEffect

    rows = session.execute(
        select(BrokerSideEffect.kind, BrokerSideEffect.status)
        .join(WorkerRun, WorkerRun.id == BrokerSideEffect.run_id)
        .where(WorkerRun.task_id == task_id, BrokerSideEffect.status.in_(["PENDING", "UNKNOWN"])))
    return [(k, st) for k, st in rows]


def _fan_out_problem(task: TaskSpec) -> str | None:
    tools = {str(t).split(":", 1)[0].lower() for t in (task.tools_allowed or [])}
    if tools & {"task", "subagent", "spawn", "delegate"}:
        return "TaskSpec requests nested agents/fan-out (not allowed, master s19/s21)"
    scope = task.capability_scope or {}
    if scope.get("fan_out") or scope.get("subtasks"):
        return "TaskSpec capability scope requests fan-out (not allowed)"
    return None


def _readiness_reasons(session: Session, te: TaskExecution, task: TaskSpec, *, approved_ok: bool, approval_reason: str,
                       by_key: dict[str, TaskSpec], te_by_task: dict[str, TaskExecution],
                       dag: dict[str, str]) -> list[tuple[str, str]]:
    reasons: list[tuple[str, str]] = []
    if not approved_ok:
        reasons.append(("PLAN_NOT_APPROVED", approval_reason or "plan version is not the ACTIVE approved version"))
    if task.task_key in dag:
        reasons.append(("INVALID_DAG", dag[task.task_key]))
    for dep_key in task.depends_on or []:
        dep = by_key.get(dep_key)
        if dep is None:
            continue
        dep_te = te_by_task.get(dep.id)
        if dep_te is None or dep_te.state != S.DONE.value:
            reasons.append(("DEPENDENCY_NOT_ACCEPTED", f"dependency {dep_key} is {dep_te.state if dep_te else 'not queued'}"))
            continue
        ok, why = ext.acceptance_valid(session, dep)
        if not ok:
            reasons.append(("DEPENDENCY_NOT_ACCEPTED", f"dependency {dep_key}: {why}"))
    fan = _fan_out_problem(task)
    if fan:
        reasons.append(("FAN_OUT_FORBIDDEN", fan))
    from aeos.workforce import roles

    if task.role_key not in roles.executor_role_keys():
        reasons.append(("ROLE_UNKNOWN", f"role_key {task.role_key!r} is not an executor RoleSkillPack (a title grants "
                                        "nothing; the task needs a new plan version with a catalog role)"))
    if te.attempts_used >= max_attempts(task, te):
        reasons.append(("NEEDS_DECISION", f"attempt cap reached ({te.attempts_used}/{max_attempts(task, te)})"))
    alive = _previous_worker_alive(session, task.id)
    if alive is not None:
        reasons.append(("LEASE_LOST_PROCESS_ALIVE", f"previous worker of run {alive.id} (pid {alive.pid}) is still alive"))
    pending = _unreconciled_side_effects(session, task.id)
    if pending:
        reasons.append(("SIDE_EFFECT_UNRECONCILED", "external side effect(s) of an earlier attempt not reconciled: "
                        + ", ".join(f"{k} {st}" for k, st in pending[:5])))
    if te.reason_code == "QUOTA_EXHAUSTED" and te.state == S.BLOCKED.value:
        wait = _quota_wait(session, te)
        if wait:
            reasons.append(("QUOTA_EXHAUSTED", wait))
    if te.state == S.BLOCKED.value and te.reason_code in HUMAN_BLOCK_CODES:
        # A quarantined patch waits for its special review, a BLOCKED review verdict for evidence or a
        # decision (§14 "REVIEW --> BLOCKED: bukti atau keputusan kurang", §21). Starting a new attempt
        # on the next tick would silently discard that; only an owner action (retry/release) clears it.
        reasons.insert(0, (te.reason_code, te.reason or "waiting for an owner action (retry / special review)"))
    return reasons


def route_quota_exhausted(session: Session, route_key: str | None) -> str | None:
    """Observed quota exhaustion (e.g. upstream 429 recorded by the broker) that has not reset yet.
    §22/§42: wait/back off on the same approved route — never switch to a paid/unapproved one."""
    from aeos.models.cost import QuotaState

    if not route_key:
        return None
    now = utcnow()
    for q in session.scalars(select(QuotaState).where(QuotaState.scope == route_key,
                                                      QuotaState.state == "EXHAUSTED")):
        resets = ensure_aware(q.resets_at)
        if resets is None or resets > now:
            until = resets.isoformat() if resets else "UNKNOWN (no reset time reported)"
            return f"quota for {route_key} exhausted until {until}"
    return None


def _quota_wait(session: Session, te: TaskExecution) -> str | None:
    run = session.get(WorkerRun, te.active_run_id) if te.active_run_id else None
    if run is None or not run.model_route_key:
        return None
    return route_quota_exhausted(session, run.model_route_key)


def refresh_readiness(session: Session, plan_version_id: str, *, actor: str = "system:scheduler") -> dict[str, int]:
    """QUEUED/BLOCKED -> READY when every dependency is DONE with a still-valid acceptance AND the
    version is the plan's ACTIVE approved version; otherwise BLOCKED / annotated with reasons.
    External tracker "Done" (OperationalWorkProjection) is never consulted (§14, §50)."""
    version = session.get(PlanVersion, plan_version_id)
    if version is None:
        raise NotFound("plan version not found")
    approved, why = ext.approved_version(session, version.plan_id)
    approved_ok = approved is not None and approved.id == plan_version_id
    if approved is not None and not approved_ok:
        why = f"approved version is {approved.id}, not {plan_version_id}"
    tasks = list(session.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == plan_version_id)))
    by_key = {t.task_key: t for t in tasks}
    tes = {te.task_id: te for te in session.scalars(
        select(TaskExecution).where(TaskExecution.task_id.in_([t.id for t in tasks])))}
    dag = validate_dag(tasks)
    counts = {"ready": 0, "blocked": 0, "unchanged": 0}
    for t in tasks:
        te = tes.get(t.id)
        if te is None or te.state not in (S.QUEUED.value, S.BLOCKED.value, S.READY.value):
            continue
        reasons = _readiness_reasons(session, te, t, approved_ok=approved_ok, approval_reason=why, by_key=by_key,
                                     te_by_task=tes, dag=dag)
        text = "; ".join(r for _, r in reasons)
        if te.state == S.QUEUED.value:
            if reasons:
                transition_task(session, t.id, S.BLOCKED.value, reason=text, actor=actor, reason_code=reasons[0][0])
                counts["blocked"] += 1
            else:
                transition_task(session, t.id, S.READY.value, reason="dependencies accepted and plan approved",
                                actor=actor)
                counts["ready"] += 1
        elif te.state == S.BLOCKED.value:
            if not reasons:
                transition_task(session, t.id, S.READY.value, reason=f"blocker resolved ({te.reason_code})", actor=actor)
                counts["ready"] += 1
            elif te.reason_code in HUMAN_BLOCK_CODES:
                # The owner-only block keeps its own code and reason: a transient co-reason (unreconciled
                # side effect, process still alive, attempt cap) must never overwrite it, or the task would
                # become READY without the special review once that co-reason cleared.
                counts["unchanged"] += 1
            else:
                _set_wait_reason(session, te, reasons[0][0], text)
                counts["unchanged"] += 1
        else:  # READY: never moves back (no READY->BLOCKED edge in §14) — admission re-checks
            if reasons:
                _set_wait_reason(session, te, reasons[0][0], text)
            elif te.reason_code in ("PLAN_NOT_APPROVED", "DEPENDENCY_NOT_ACCEPTED", "INVALID_DAG"):
                _set_wait_reason(session, te, "", "")
            counts["unchanged"] += 1
        tes[t.id] = get_te(session, t.id)
    return counts


def active_plan_versions(session: Session) -> list[str]:
    rows = session.scalars(select(TaskExecution.plan_version_id).where(
        TaskExecution.state.in_([S.QUEUED.value, S.BLOCKED.value, S.READY.value, S.REVIEW.value]),
        TaskExecution.mode == DataMode.LIVE.value, TaskExecution.plan_version_id.is_not(None)).distinct())
    return [r for r in rows if r]


# ----------------------------------------------------------------------------- admission (§19)
@dataclass
class Admission:
    task_id: str
    admitted: bool = False
    waits: list[tuple[str, str]] = field(default_factory=list)
    blocks: list[tuple[str, str]] = field(default_factory=list)
    route: ModelRoute | None = None
    agent: AgentDefinition | None = None
    est_usd: float = 0.0
    est_tokens: int = 0
    cap: int = 1
    # §19 admission answers recorded by code: context size, compute reservation, verifier/reviewer path,
    # role eligibility (never a narrative from a model)
    answers: dict[str, Any] = field(default_factory=dict)

    @property
    def reasons(self) -> list[str]:
        return [f"{c}: {m}" for c, m in [*self.blocks, *self.waits]]

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "admitted": self.admitted, "blocks": self.blocks, "waits": self.waits,
                "route": self.route.route_key if self.route else None,
                "agent": self.agent.agent_key if self.agent else None, "cap": self.cap, "answers": self.answers}


def active_executions(session: Session, *, exclude_task: str | None = None) -> list[TaskExecution]:
    q = select(TaskExecution).where(TaskExecution.state.in_([s.value for s in ACTIVE_STATES]),
                                    TaskExecution.mode == DataMode.LIVE.value)
    rows = list(session.scalars(q))
    return [r for r in rows if r.task_id != exclude_task]


# §19 "Auth/IAM/shared schema/migration/control-plane policy -> serialise decisions and shared mutations":
# two active tasks touching the same class conflict even when their files differ (two migration files can
# create two Alembic heads; two auth/policy edits interact). Matched on the static path prefix, case-insensitive.
SERIALISATION_CLASSES: dict[str, tuple[str, ...]] = {
    "migrations": ("migrations/", "alembic/", "db/migrations/"),
    "schema-models": ("backend/aeos/models/", "models/", "schema/", "prisma/"),
    "auth-iam": ("backend/aeos/identity/", "backend/aeos/api/deps.py", "auth/", "iam/"),
    "control-plane-policy": ("policy/", "backend/aeos/policy/"),
    "contracts": ("contracts/",),
    "pipeline": (".github/workflows/", "bitbucket-pipelines.yml"),
}


def serialisation_classes(paths: list[str]) -> set[str]:
    from aeos.worker_runtime.scope import static_prefix

    out: set[str] = set()
    for p in paths:
        prefix = (static_prefix(str(p)) or "").replace("\\", "/").casefold()
        probe = prefix + ("" if prefix.endswith((".py", ".yml", ".yaml")) else "/")
        for cls, roots in SERIALISATION_CLASSES.items():
            for root in roots:
                r = root.casefold()
                if probe.startswith(r) or (r.endswith("/") and f"/{r}" in f"/{probe}") or (prefix and r.startswith(probe)):
                    out.add(cls)
    return out


def write_set_conflicts(session: Session, task: TaskSpec) -> list[dict[str, Any]]:
    from aeos.worker_runtime.scope import write_sets_overlap

    out = []
    mine = serialisation_classes(list(task.allowed_paths or []))
    for other in active_executions(session, exclude_task=task.id):
        ot = session.get(TaskSpec, other.task_id)
        if ot is None or ot.project_id != task.project_id:
            continue
        pairs = write_sets_overlap(list(task.allowed_paths or []), list(ot.allowed_paths or []))
        shared = sorted(mine & serialisation_classes(list(ot.allowed_paths or [])))
        if pairs or shared:
            out.append({"task_id": ot.id, "task_key": ot.task_key, "pairs": pairs[:5], "classes": shared})
    return out


def scope_contract_problems(session: Session, task: TaskSpec) -> list[tuple[str, str]]:
    """Re-check the TaskSpec write scope at lease time (§21, §50 "File path traversal/symlink keluar
    sandbox"): every allowed/forbidden glob must be a safe repository-relative path
    (``aeos.planning.dag.unsafe_path``), and an allowed area that lies inside a forbidden one is a
    contradictory contract. Forbidden matching is case-insensitive: Windows and macOS file systems are
    case-insensitive, so ``Policy/**`` must never slip past ``policy/**``."""
    from aeos.worker_runtime.scope import ScopePathError, matches, normalize_pattern, static_prefix

    try:
        from aeos.planning.dag import unsafe_path
    except ImportError:  # planning module absent: the local normaliser below still fails closed
        def unsafe_path(p: Any) -> str | None:
            return None

    problems: list[tuple[str, str]] = []
    allowed = [str(p) for p in (task.allowed_paths or [])]
    forbidden = [str(p) for p in (task.forbidden_paths or [])]
    if not allowed:
        # ADR-0033 / §21 "allowed paths": an empty write scope would tell the worker the whole repository is
        # writable; it is refused before any budget or attempt is spent.
        problems.append(("NO_ALLOWED_PATHS", "TaskSpec has no allowed_paths; a worker is never started without an "
                                             "explicit write scope (ADR-0033)"))
    for label, paths in (("allowed", allowed), ("forbidden", forbidden)):
        for p in paths:
            why = unsafe_path(p)
            if why is None:
                try:
                    normalize_pattern(p)
                except ScopePathError as exc:
                    why = str(exc)
            if why:
                problems.append(("UNSAFE_PATH", f"{label} path {p!r} is not a safe repository-relative path: {why}"))
    if problems:
        return problems
    for a in allowed:
        prefix = static_prefix(a)
        probes = [prefix, f"{prefix}/__aeos_probe__"] if prefix else []
        for f in forbidden:
            if any(matches(probe, f, case_insensitive=True) for probe in probes):
                problems.append(("FORBIDDEN_PATH_ALLOWED", f"allowed path {a!r} lies inside forbidden path {f!r} "
                                 "(compared case-insensitively); the TaskSpec contract is contradictory"))
    return problems


def _cost_estimate(task: TaskSpec, route: ModelRoute) -> tuple[float | None, int]:
    limits = task.limits or {}
    tokens = int(limits.get("max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS)
    if route.is_free == "YES":
        return 0.0, tokens
    if limits.get("max_cost_usd") is not None:
        return float(limits["max_cost_usd"]), tokens
    return None, tokens


# ----------------------------------------------------------------------------- §19 admission answers (C7)
KEY_ROLE_EVAL_FROM = "workforce.role_eval_required_from"  # risk class from which a PASS role evaluation is needed
DEFAULT_ROLE_EVAL_FROM = "R4"
RISK_ORDER = ("R0", "R1", "R2", "R3", "R4")
CHARS_PER_TOKEN = 4
ROLE_EVAL_TARGET_TYPE = "role_evaluation"


def role_eligibility(session: Session, task: TaskSpec) -> dict[str, Any]:
    """§46 "agent menjadi eligible untuk role dari hasil evaluasi, bukan dari judul di prompt". From the risk
    class in ``workforce.role_eval_required_from`` (default R4) a PASS evaluation of the role's current revision,
    or an ACTIVE owner EXCEPTION on role_evaluation/<role_key> bound to that revision, is required."""
    from aeos.workforce import roles

    threshold = str(_setting(session, KEY_ROLE_EVAL_FROM, DEFAULT_ROLE_EVAL_FROM) or DEFAULT_ROLE_EVAL_FROM)
    risk = task.risk_class if task.risk_class in RISK_ORDER else "R1"
    required = RISK_ORDER.index(risk) >= RISK_ORDER.index(threshold if threshold in RISK_ORDER else "R4")
    revision = roles.role_revision(session, task.role_key)
    evaluation = roles.role_evaluation(session, task.role_key, revision) if revision else {
        "status": "NOT_EVALUATED", "basis": "role is not a catalog executor"}
    out = {"required": required, "threshold": threshold, "risk_class": risk, "role_key": task.role_key,
           "role_revision_hash": revision, "status": evaluation["status"], "basis": evaluation.get("basis", "")}
    if required and evaluation["status"] != "PASS" and revision:
        exc = ext.active_exception_decision(session, target_type=ROLE_EVAL_TARGET_TYPE, target_id=task.role_key,
                                            target_hash=revision)
        out["exception_decision_id"] = exc.id if exc else None
    # §46 stated, never implied: what this admission's role eligibility actually rests on
    if evaluation["status"] == "PASS":
        out["eligibility_basis"] = "EVALUATION_RESULT"
    elif out.get("exception_decision_id"):
        out["eligibility_basis"] = "OWNER_EXCEPTION"
    elif required:
        out["eligibility_basis"] = "NOT_ELIGIBLE"
    else:
        out["eligibility_basis"] = "PILOT_THRESHOLD_UNEVALUATED"
        out["note"] = (f"Role {task.role_key} belum dievaluasi ({evaluation['status']}); risiko {risk} di bawah ambang "
                       f"{threshold}, jadi diizinkan oleh mode pilot - BUKAN dari hasil evaluasi. Fixture/grader kasus "
                       "suite role disusun pemilik (ADR-0060); ambang dapat diperketat lewat "
                       "POST /api/workforce/role-eval-threshold.")
    return out


def set_role_eval_threshold(session: Session, *, risk_class: str, actor: str, rationale: str) -> dict[str, Any]:
    """Owner action (§46 "eligible dari hasil evaluasi"): from which risk class a PASS role evaluation (or an owner
    EXCEPTION) is required at admission. R4 is the pilot default (the loosest); a lower class only tightens
    admission, so no decision is needed - the change is audited and emitted like the other scheduler settings."""
    if risk_class not in RISK_ORDER:
        raise ValidationFailed(f"risk_class must be one of {list(RISK_ORDER)}", code="INVALID_RISK_CLASS")
    if len((rationale or "").strip()) < 5:
        raise ValidationFailed("rationale is required", code="RATIONALE_REQUIRED")
    previous = str(_setting(session, KEY_ROLE_EVAL_FROM, DEFAULT_ROLE_EVAL_FROM) or DEFAULT_ROLE_EVAL_FROM)
    _set_setting(session, KEY_ROLE_EVAL_FROM, risk_class)
    _audit(session, actor, "workforce.role_eval_threshold", target_type="scheduler", target_id=KEY_ROLE_EVAL_FROM,
           project_id=None, domain=None, reason=redact_text(rationale)[:500],
           details={"previous": previous, "threshold": risk_class})
    events.emit(session, source="controller", kind="scheduler.role_eval_threshold_changed", entity_type="scheduler",
                entity_id="scheduler", payload={"previous": previous, "threshold": risk_class})
    return {"previous": previous, "threshold": risk_class}


def _prompt_context(session: Session, task: TaskSpec, te: TaskExecution) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The ContextPack the runtime would hand this worker (a CURRENT task-scoped pack for the pinned base commit).
    Before the base commit is pinned (first attempt) the CURRENT pack counts as copied: an upper bound."""
    from aeos.worker_runtime import executor

    base = te.base_commit or task.base_commit
    pack, info = executor._context_pack(session, task, base or "")
    if pack is None and not base and info.get("freshness") == "CURRENT" and info.get("base_commit"):
        pack, info = executor._context_pack(session, task, str(info["base_commit"]))
    return pack, info


def context_estimate(session: Session, task: TaskSpec, te: TaskExecution, route: ModelRoute | None) -> dict[str, Any]:
    """§19 "berapa konteks yang harus disalin": exactly the prompt the runtime copies into the sandbox - TaskSpec,
    role contract, protected paths, repair findings, the resolved input-artifact content and the CURRENT task
    ContextPack (both within the runtime's own character caps) - estimated at ~4 characters per token, plus the
    output cap, against the route's context window."""
    try:
        from aeos.models.portfolio import Project
        from aeos.worker_runtime import executor
        from aeos.worker_runtime.scope import GENERIC_PROTECTED_PATHS, platform_protected_paths
        from aeos.workforce import roles

        project = session.get(Project, task.project_id)
        protected = (platform_protected_paths() if project is not None and project.is_platform
                     else GENERIC_PROTECTED_PATHS)
        artifacts = executor.resolve_artifacts(session, task)
        pack, pack_info = _prompt_context(session, task, te)
        prompt = executor.build_prompt(task, findings=list(te.pending_findings or []), protected=tuple(protected),
                                       branch="aeos/estimate", context_pack=pack, artifacts=artifacts,
                                       role_contract=roles.role_contract(session, task.role_key))
    except Exception:
        return {"status": "UNKNOWN", "reason": "prompt estimate unavailable"}
    chars = len(prompt)
    prompt_tokens = chars // CHARS_PER_TOKEN + 1
    output_cap = int((task.limits or {}).get("max_output_tokens", 8192) or 8192)
    minimum = int((task.model_route_policy or {}).get("min_context_tokens") or 0)
    window = route.context_window if route is not None else None
    needed = max(prompt_tokens + output_cap, minimum)
    copied = {"prompt_chars": chars, "attachments": len(artifacts), "context_pack_id": pack_info.get("pack_id"),
              "context_pack_used": bool(pack_info.get("used"))}
    if not window:
        return {"status": "UNKNOWN", "prompt_tokens": prompt_tokens, "output_cap": output_cap, "needed": needed,
                "context_window": None, "reason": "route context window UNKNOWN", **copied}
    return {"status": "FITS" if needed <= window else "TOO_LARGE", "prompt_tokens": prompt_tokens,
            "output_cap": output_cap, "min_context_tokens": minimum, "needed": needed, "context_window": window,
            **copied}


# §22 host capacity for worker runs: physical memory minus a fixed host reserve for the OS, the controller and the
# supervisor (at least HOST_RESERVE_MIN_MB or HOST_RESERVE_SHARE of the host, whichever is larger).
HOST_RESERVE_MIN_MB = 2048
HOST_RESERVE_SHARE = 0.25


def run_compute_limits(task: Any, *, sandbox_kind: str | None) -> dict[str, Any]:
    """The per-run compute the runtime ENFORCES for this TaskSpec - the same values admission reserves (§19, §22):
    memory = ``processes.ResourceLimits.from_task`` (the process watchdog's RSS cap and the Windows job backstop,
    or docker ``--memory`` in the container sandbox); CPUs = ``limits.max_cpus`` (docker ``--cpus`` in the container
    sandbox; the process sandbox has no CPU cap, so there the CPU count is a reservation only)."""
    from aeos.worker_runtime import facade as runtime

    enforced = runtime.enforced_run_limits(getattr(task, "limits", None) or {}, sandbox_kind=sandbox_kind)
    memory_mb, cpus = enforced["memory_mb"], float(enforced["cpus"])
    return {"memory_mb": int(memory_mb), "cpus": max(cpus, 0.1), "sandbox_kind": sandbox_kind or "process",
            "memory_enforced": True, "cpu_enforced": bool(enforced["cpu_enforced"]),
            "enforced_by": ("docker --memory/--cpus" if sandbox_kind == "container"
                            else "runtime watchdog RSS cap (+ Windows job memory limit); no CPU cap")}


def _reserved_compute(session: Session, *, exclude_task: str | None) -> tuple[int, float]:
    """Memory (MB) and CPUs reserved by runs that hold a slot (LEASED or RUNNING)."""
    rows = session.scalars(select(WorkerRun).where(WorkerRun.execution_status.in_([S.LEASED.value, S.RUNNING.value]),
                                                   WorkerRun.mode == DataMode.LIVE.value))
    mem, cpu = 0, 0.0
    for r in rows:
        if r.task_id == exclude_task:
            continue
        c = (r.caps or {}).get("compute") or {}
        mem += int(c.get("memory_mb") or 0)
        cpu += float(c.get("cpus") or 0)
    return mem, cpu


def _reserved_compute_mb(session: Session, *, exclude_task: str | None) -> int:
    return _reserved_compute(session, exclude_task=exclude_task)[0]


def compute_check(session: Session, task: Any, *, active: int, cap: int, sandbox_kind: str | None = None
                  ) -> dict[str, Any]:
    """§19 "batas resource/biaya" + §22 "mencadangkan compute": the memory and CPUs the runtime enforces for this run
    (``run_compute_limits``) are reserved against the host capacity (physical memory minus the host reserve; the
    logical CPU count) minus what LEASED/RUNNING runs already reserved - a deterministic capacity, not the momentary
    free memory of other processes (that is recorded as an observation and a warning). Admission therefore never
    reserves less than a run may consume before the runtime stops it."""
    import psutil

    lim = run_compute_limits(task, sandbox_kind=sandbox_kind)
    need_mb = lim["memory_mb"]
    try:
        vm = psutil.virtual_memory()
        total_mb = int(vm.total // (1024 * 1024))
        available_mb = int(getattr(vm, "available", 0) // (1024 * 1024))
        cpus = int(psutil.cpu_count(logical=True) or 1)
    except Exception:
        return {"status": "UNKNOWN", **lim, "reason": "host resource probe failed"}
    budget = max(0, total_mb - max(HOST_RESERVE_MIN_MB, int(total_mb * HOST_RESERVE_SHARE)))
    reserved, reserved_cpus = _reserved_compute(session, exclude_task=task.id)
    need_cpus = min(lim["cpus"], float(cpus))  # a run asking more CPUs than the host has gets all of them
    out: dict[str, Any] = {
        **lim, "cpus": need_cpus, "worker_budget_mb": budget, "reserved_mb": reserved, "reserved_cpus": reserved_cpus,
        "available_mb_observed": available_mb, "host_cpus": cpus, "enforced": True,
        "note": "reserved = the cap the runtime enforces for this run"}
    if available_mb < need_mb:
        out["warning"] = f"host free memory {available_mb} MB is below this run's reservation ({need_mb} MB) right now"
    if budget - reserved < need_mb:
        return {**out, "status": "INSUFFICIENT", "reason": f"{budget - reserved} MB of the worker memory budget left "
                                                           f"after reservations, {need_mb} MB needed"}
    if reserved_cpus + need_cpus > cpus:
        return {**out, "status": "INSUFFICIENT", "reason": f"{reserved_cpus:g} CPU(s) reserved by active runs + "
                                                           f"{need_cpus:g} needed exceed the host's {cpus}"}
    return {**out, "status": "OK"}


def verifier_path(session: Session, task: TaskSpec) -> dict[str, Any]:
    """§19 "siapa verifier dan penerima hasilnya": the trusted suite that will judge the result and the reviewer
    path (native Claude Max review when CAN_REVIEW_WITH_MAX is READY, otherwise the manual review package)."""
    from aeos.models.portfolio import Project

    out: dict[str, Any] = {"verifier": "independent-verifier (trusted suite outside the candidate)"}
    project = session.get(Project, task.project_id)
    try:
        from aeos.verification import suite

        st = suite.suite_status(session, project) if project is not None else {"status": "BLOCKED",
                                                                                 "code": "PROJECT_NOT_FOUND"}
    except ImportError as exc:
        st = {"status": "BLOCKED", "code": "MODULE_UNAVAILABLE", "reason": _str(exc)}
    out["suite"] = st
    ok, _reasons = ext.capability_ready(session, Capability.CAN_REVIEW_WITH_MAX, domain=task.domain,
                                        project_id=task.project_id)
    out["reviewer_path"] = "native (CAN_REVIEW_WITH_MAX READY)" if ok else "manual review package (official Claude Code)"
    out["result_receiver"] = "verification -> checkpoint review -> AcceptanceRecord (owner merges)"
    return out


def _str(v: Any) -> str:
    return redact_text(str(v))[:300]


def _agent_scope_problem(agent: AgentDefinition | None, task: TaskSpec) -> str | None:
    """§35 "project-scoped role/agent definitions": an agent outside its allowed domains/projects never runs."""
    if agent is None:
        return None
    scope = agent.allowed_scope or {}
    domains = scope.get("domains")
    if isinstance(domains, list) and domains and task.domain not in domains:
        return f"agent {agent.agent_key} is not allowed in domain {task.domain}"
    projects = scope.get("project_ids")
    if isinstance(projects, list) and task.project_id not in projects:
        return f"agent {agent.agent_key} is not allowed for this project"
    return None


def _agent_permits(agent: AgentDefinition, tool: str) -> bool:
    """bash/webfetch: allowed by the agent's tool list, or by a limit that defers to the TaskSpec
    (``deny unless TaskSpec...``). Anything else (absent or ``deny``) never permits it."""
    if tool in (agent.tools or []):
        return True
    rule = str((agent.limits or {}).get(tool) or "deny").strip().lower()
    return rule.startswith("deny unless taskspec") or rule == "allow"


def agent_configuration_problems(agent: AgentDefinition | None, task: TaskSpec,
                                 route: ModelRoute | None) -> list[tuple[str, str]]:
    """§17 "AgentDefinition: konfigurasi worker (role, harness, route policy, tools, limits, owner)": the definition
    CONFIGURES the run rather than only describing it - the runtime's tool grant and the TaskSpec's bash/webfetch
    requests must lie inside the agent's tools/limits, and the selected route must satisfy the agent's
    route_policy (pool, optional allowed_routes). A run outside the definition is never admitted."""
    if agent is None:
        return []
    problems: list[tuple[str, str]] = []
    from aeos.worker_runtime.opencode import BASE_ALLOW

    missing = sorted(set(BASE_ALLOW) - set(agent.tools or []))
    if missing:
        problems.append(("AGENT_TOOLS_INSUFFICIENT", f"agent {agent.agent_key} does not allow the runtime tools "
                                                     f"{missing} every worker run receives"))
    asked = [str(t) for t in task.tools_allowed or []]
    for tool, wanted in (("bash", any(t == "bash" or t.startswith("bash:") for t in asked)),
                         ("webfetch", "webfetch" in asked)):
        if wanted and not _agent_permits(agent, tool):
            problems.append(("AGENT_TOOL_NOT_ALLOWED", f"TaskSpec asks for {tool} but agent {agent.agent_key} "
                                                       f"does not permit it"))
    if route is not None:
        policy = agent.route_policy or {}
        pool = policy.get("pool")
        if pool and route.pool != pool:
            problems.append(("AGENT_ROUTE_POLICY", f"route {route.route_key} is in pool {route.pool}; agent "
                                                   f"{agent.agent_key} runs only on {pool} routes"))
        allowed = policy.get("allowed_routes")
        if isinstance(allowed, list) and allowed and route.route_key not in allowed:
            problems.append(("AGENT_ROUTE_POLICY", f"route {route.route_key} is not in agent {agent.agent_key}'s "
                                                   "allowed_routes"))
    return problems


def admission_check(session: Session, task_id: str) -> Admission:
    te = get_te(session, task_id)
    task = session.get(TaskSpec, task_id)
    adm = Admission(task_id=task_id)
    if task is None:
        adm.blocks.append(("NOT_FOUND", "task spec missing"))
        return adm
    if te.state != S.READY.value:
        adm.blocks.append(("NOT_READY", f"task is {te.state}"))
        return adm
    fixture = task_data_mode(task, session.get(PlanVersion, task.plan_version_id))
    if fixture or te.mode != DataMode.LIVE.value:
        # §35/§50 "Fixture roster/run ... MOCK/DEMO jelas; tidak dihitung accepted outcome/cost live":
        # fixture content is never executed by a real worker against a real repository and budget.
        adm.blocks.append(("NON_LIVE_TASKSPEC", f"TaskSpec/plan version is {fixture or te.mode}; fixture content "
                                                "is never admitted as live work"))
    for code, why in scope_contract_problems(session, task):
        adm.blocks.append((code, why))
    if not [o for o in (task.expected_outputs or []) if str(o).strip()]:
        # §19 "output tambahan apa yang dihasilkan agent": a run without a declared output is never started
        adm.blocks.append(("NO_EXPECTED_OUTPUT", "TaskSpec declares no expected_outputs"))
    from aeos.workforce import roles

    if task.role_key not in roles.executor_role_keys():
        # §19 diagram "katalog role/skill -> task compiler", §17 "RoleSkillPack yang relevan": a title that is
        # not a catalog executor role grants nothing and is never run
        adm.blocks.append(("ROLE_UNKNOWN", f"role_key {task.role_key!r} is not an executor RoleSkillPack"))
    else:
        elig = role_eligibility(session, task)
        adm.answers["role_eligibility"] = elig
        if elig["required"] and elig["status"] != "PASS" and not elig.get("exception_decision_id"):
            adm.waits.append(("ROLE_NOT_EVALUATED", f"risk {elig['risk_class']} >= {elig['threshold']}: role "
                              f"{task.role_key} needs a PASS evaluation of revision {str(elig['role_revision_hash'])[:19]} "
                              f"({elig['status']}: {elig['basis']}) or an owner EXCEPTION"))
    if scheduling_paused(session):
        adm.waits.append(("SCHEDULING_PAUSED", "scheduling is paused by the owner"))
    approved, why = ext.approved_version(session, te.plan_id)
    if approved is None or approved.id != task.plan_version_id:
        adm.waits.append(("PLAN_NOT_APPROVED", why or "task version is not the approved version"))
    for dep_key in task.depends_on or []:
        dep = session.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == task.plan_version_id,
                                                    TaskSpec.task_key == dep_key))
        dep_te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == dep.id)) if dep else None
        ok = bool(dep and dep_te and dep_te.state == S.DONE.value and ext.acceptance_valid(session, dep)[0])
        if not ok:
            adm.waits.append(("DEPENDENCY_NOT_ACCEPTED", f"dependency {dep_key} not DONE with valid acceptance"))
    if te.attempts_used >= max_attempts(task, te):
        adm.blocks.append(("ATTEMPTS_EXHAUSTED", f"{te.attempts_used}/{max_attempts(task, te)} attempts used"))
    fan = _fan_out_problem(task)
    if fan:
        adm.blocks.append(("FAN_OUT_FORBIDDEN", fan))
    agent_key = te.assigned_agent_key or IMPLEMENTER_AGENT_KEY
    agent = session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    if agent is None or agent.harness != "opencode" or agent_key in REVIEWER_AGENT_KEYS:
        adm.waits.append(("AGENT_DISABLED", f"agent {agent_key} is not an implementation agent"))
    elif not agent.enabled:
        adm.waits.append(("AGENT_DISABLED", f"agent {agent_key} is disabled: {agent.enabled_reason}"))
    out_of_scope = _agent_scope_problem(agent, task)
    if out_of_scope:
        adm.blocks.append(("AGENT_OUT_OF_SCOPE", out_of_scope))
    adm.agent = agent
    if task.domain == "OFFICE":
        ok, why = ext.office_external_allowed(session)
        if not ok:
            adm.waits.append(("OFFICE_DENIED", why))
    ok, reasons = ext.capability_ready(session, Capability.CAN_RUN_WORKER, domain=task.domain, project_id=task.project_id)
    if not ok:
        adm.waits.append(("CAPABILITY_NOT_READY", "; ".join(reasons)))
    from aeos.worker_runtime.sandbox import task_sandbox_readiness

    # §25: the boundary THIS TaskSpec needs (domain, risk tier R3/R4, shell and egress grants); a shell is
    # never given under the WEAK process sandbox (ADR-0051 amendment 2026-10-19).
    sb = task_sandbox_readiness(session, domain=task.domain, risk_class=task.risk_class,
                                tools_allowed=list(task.tools_allowed or []),
                                egress_allowed=list(task.egress_allowed or []))
    if sb["status"] != "READY":
        adm.waits.append(("SANDBOX_NOT_READY", "; ".join(sb["reasons"])))
    cap, _decision = concurrency_cap(session, task)
    if agent is not None:
        try:  # AgentDefinition.limits are an upper bound on the scheduler's own cap (§17)
            cap = max(1, min(cap, int((agent.limits or {}).get("max_active", cap))))
        except (TypeError, ValueError):
            cap = 1
    adm.cap = cap
    active = active_executions(session, exclude_task=task.id)
    if len(active) >= cap:
        adm.waits.append(("CONCURRENCY_CAP", f"{len(active)} active implementation worker(s); cap {cap}"))
    compute = compute_check(session, task, active=len(active), cap=cap, sandbox_kind=sb.get("kind"))
    adm.answers["compute"] = compute
    if compute["status"] == "INSUFFICIENT":
        adm.waits.append(("RESOURCE_INSUFFICIENT", compute["reason"]))
    conflicts = write_set_conflicts(session, task)
    if conflicts:
        adm.waits.append(("SHARED_WRITE_CONFLICT", "shared-write conflict with " +
                          ", ".join(c["task_key"] for c in conflicts) + " (serialised)"))
    # §14/§50 "Controller crash dan worker lama masih aktif": no new attempt while an earlier
    # attempt's process or one of its external side effects may still be active/unreconciled.
    alive = _previous_worker_alive(session, task.id)
    if alive is not None:
        adm.waits.append(("LEASE_LOST_PROCESS_ALIVE",
                          f"previous worker of run {alive.id} (pid {alive.pid}) is still alive"))
    pending = _unreconciled_side_effects(session, task.id)
    if pending:
        adm.waits.append(("SIDE_EFFECT_UNRECONCILED", "external side effect(s) of an earlier attempt not "
                          "reconciled: " + ", ".join(f"{k} {st}" for k, st in pending[:5])))
    ok, why = ext.verifier_available()
    if not ok:
        adm.waits.append(("VERIFIER_UNAVAILABLE", f"no independent verifier/acceptance path: {why}"))
    else:
        vp = verifier_path(session, task)
        adm.answers["verifier"] = vp
        if vp["suite"].get("code") in ("TRUSTED_SUITE_TAMPERED", "TRUSTED_SUITE_MISSING"):
            # a registered suite that no longer verifies would end every attempt BLOCKED after spending budget
            adm.waits.append(("VERIFIER_UNAVAILABLE", f"trusted suite {vp['suite']['code']}: {vp['suite'].get('reason')}"))
    route, rreasons = ext.select_route(session, task)
    implementer = agent if agent is not None and agent.harness == "opencode" and \
        agent.agent_key not in REVIEWER_AGENT_KEYS else None
    for code, why in agent_configuration_problems(implementer, task, route):
        adm.blocks.append((code, why))
    if route is None:
        adm.waits.append(("ROUTE_INELIGIBLE", "; ".join(rreasons)))
    else:
        adm.route = route
        if task.domain == "OFFICE":
            # §23 controlled path: the route's owner decision (company authorisation + company-approved route,
            # bound to the route identity) must still hold at admission — a revoked decision ends it here.
            ok, why = ext.office_route_authorised(session, route)
            if not ok:
                adm.waits.append(("OFFICE_ROUTE_NOT_AUTHORISED", why))
        ctx = context_estimate(session, task, te, route)
        adm.answers["context"] = ctx
        if ctx["status"] == "TOO_LARGE":
            adm.blocks.append(("CONTEXT_TOO_LARGE", f"~{ctx['needed']} tokens (prompt + output cap) exceed the "
                                                    f"route context window {ctx['context_window']}"))
        quota = route_quota_exhausted(session, route.route_key)
        if quota:
            adm.waits.append(("QUOTA_EXHAUSTED", quota + "; waiting (no fallback to another route)"))
        est_usd, est_tokens = _cost_estimate(task, route)
        if est_usd is None:
            adm.waits.append(("COST_UNKNOWN", f"route {route.route_key} is not free and the TaskSpec has no "
                                              "limits.max_cost_usd; no paid run without a cost decision"))
        else:
            adm.est_usd, adm.est_tokens = est_usd, est_tokens
    # §40 trial admission: a TaskSpec launched for a change-proposal trial runs only while its TRIAL decision is
    # still bound to the live baseline/candidate/scope (provider, operations, caps) - re-checked every attempt.
    try:
        from aeos.proposals import facade as proposals

        for code, why in proposals.trial_admission_problems(session, task, route=adm.route):
            adm.blocks.append((code, why))
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - proposals ships with the foundation
        if _is_trial_task(task):
            adm.blocks.append(("MODULE_UNAVAILABLE", "proposal trial binding cannot be verified (fail closed)"))
    except AeosError as exc:  # a binding check that cannot complete blocks this trial task only (fail closed)
        adm.blocks.append(("TRIAL_BINDING_UNVERIFIED", f"{exc.code}: {exc.message}"[:300]))
    adm.admitted = not adm.waits and not adm.blocks
    return adm


def _is_trial_task(task: TaskSpec | None) -> bool:
    """A change-proposal trial task (fail-closed fallback when aeos.proposals cannot be imported). Input artifacts
    are the plan's strings or the compiler's hash-bound dicts that keep the original ``label``."""
    for a in (task.input_artifacts if task is not None else None) or []:
        label = str(a.get("label") or "") if isinstance(a, dict) else str(a)
        if label.startswith("change_proposal:"):
            return True
    return False


def _role_revision(session: Session, role_key: str | None) -> str | None:
    from aeos.models.workforce import RoleSkillPack

    if not role_key:
        return None
    role = session.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == role_key))
    return role.revision_hash if role is not None else None


def acquire_lease(session: Session, task_id: str, *, actor: str, admission: Admission | None = None,
                  owner: str = CONTROLLER_ID, ttl_seconds: int | None = None) -> WorkerRun:
    """READY -> LEASED with reservation + lease in ONE transaction (§14 "reservasi dan lease
    atomik"). Exactly one concurrent caller wins (conditional UPDATE on state + row_version)."""
    te = get_te(session, task_id)
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task spec missing")
    if te.state != S.READY.value:
        raise Conflict(f"task is {te.state}, not READY", code="NOT_READY")
    adm = admission or admission_check(session, task_id)
    if not adm.admitted:
        raise Blocked("admission refused: " + "; ".join(adm.reasons), code="ADMISSION_REFUSED",
                      details={"reasons": adm.reasons})
    assert adm.route is not None and adm.agent is not None
    run_id = new_id()
    expected_rv = te.row_version
    reservation = ext.reserve(session, run_id=run_id, project_id=task.project_id, domain=task.domain,
                              route_key=adm.route.route_key, est_usd=adm.est_usd, est_tokens=adm.est_tokens,
                              est_requests=int((task.limits or {}).get("max_requests", DEFAULT_MAX_REQUESTS)))
    now = utcnow()
    res = session.execute(
        update(TaskExecution)
        .where(TaskExecution.id == te.id, TaskExecution.state == S.READY.value, TaskExecution.row_version == expected_rv)
        .values(state=S.LEASED.value, row_version=expected_rv + 1, active_run_id=run_id,
                attempts_used=TaskExecution.attempts_used + 1, reason="lease acquired", reason_code="",
                state_changed_at=now, updated_at=now)
    )
    if res.rowcount != 1:
        ext.release(session, getattr(reservation, "id", None))
        raise Conflict("lost the lease to a concurrent admission", code="LEASE_CONFLICT")
    te = get_te(session, task_id)
    ttl = ttl_seconds or lease_ttl(session)
    lease_id = new_id()
    limits = task.limits or {}
    run = WorkerRun(
        id=run_id, task_id=task.id, project_id=task.project_id, domain=task.domain, attempt=te.attempts_used,
        run_type="IMPLEMENTATION", agent_key=adm.agent.agent_key, role_key=task.role_key,
        execution_status=S.LEASED.value, model_route_key=adm.route.route_key,
        model_resolved=adm.route.resolved_version or None, engine=adm.agent.harness, lease_id=lease_id,
        plan_version_id=task.plan_version_id, task_key=task.task_key, requested_by=te.requested_by,
        budget_reservation_id=getattr(reservation, "id", None), mode=te.mode,
        agent_revision_hash=adm.agent.revision_hash, role_revision_hash=_role_revision(session, task.role_key),
        caps={"max_requests": int(limits.get("max_requests", DEFAULT_MAX_REQUESTS)),
              "max_tokens": int(limits.get("max_tokens", DEFAULT_MAX_TOKENS)),
              "max_cost_usd": adm.est_usd, "timeout_seconds": int(limits.get("timeout_seconds", 1800)),
              "max_attempts": max_attempts(task, te), "capability_ttl_seconds": task.capability_ttl_seconds,
              "concurrency_cap": adm.cap,
              # §22 compute reserved with the slot and the cost (released when the run is no longer LEASED)
              "compute": {k: v for k, v in (adm.answers.get("compute") or {}).items()
                          if k in ("memory_mb", "cpus", "status", "enforced", "memory_enforced", "cpu_enforced",
                                   "sandbox_kind")},
              "context_estimate": adm.answers.get("context"),
              "verifier_path": adm.answers.get("verifier"),
              "role_eligibility": adm.answers.get("role_eligibility")},
    )
    session.add(run)
    session.add(Lease(id=lease_id, task_id=task.id, run_id=run_id, owner=owner, acquired_at=now,
                      expires_at=now + timedelta(seconds=ttl), heartbeat_at=now, state="ACTIVE", ttl_seconds=ttl))
    session.flush()
    _emit_task(session, te, S.READY.value, S.LEASED.value, "lease acquired", actor)
    _emit_run(session, run, S.QUEUED.value, S.LEASED.value, "lease acquired", actor)
    return run


FAIRNESS_MAX_WAIT = timedelta(minutes=30)


def fair_order(session: Session, ready: list[TaskExecution]) -> list[TaskExecution]:
    """ADR-0050 amendment (fairness, §10 "Workforce & Scheduler: fairness"):

    1. anti-starvation: a task READY for longer than ``FAIRNESS_MAX_WAIT`` goes first (oldest first);
    2. otherwise round-robin by project: projects whose last lease is oldest (or that never had one) come first,
       one task per project per round, each project's tasks oldest-READY first.
    Deterministic (ties by task id); no LLM, no priority inferred from model output."""
    now = utcnow()
    floor = now - timedelta(days=36500)

    def since(te: TaskExecution) -> Any:
        return ensure_aware(te.state_changed_at) or ensure_aware(te.created_at) or floor

    aged = sorted([te for te in ready if since(te) <= now - FAIRNESS_MAX_WAIT], key=lambda t: (since(t), t.task_id))
    rest = [te for te in ready if te not in aged]
    groups: dict[str, list[TaskExecution]] = {}
    for te in sorted(rest, key=lambda t: (since(t), t.task_id)):
        groups.setdefault(te.project_id, []).append(te)
    last: dict[str, Any] = {}
    if groups:
        rows = session.execute(select(WorkerRun.project_id, func.max(WorkerRun.created_at))
                               .where(WorkerRun.project_id.in_(list(groups)), WorkerRun.run_type == "IMPLEMENTATION")
                               .group_by(WorkerRun.project_id))
        last = {pid: ensure_aware(at) for pid, at in rows}
    order = sorted(groups, key=lambda pid: (last.get(pid) or floor, since(groups[pid][0]), pid))
    interleaved: list[TaskExecution] = []
    while any(groups[p] for p in order):
        for pid in order:
            if groups[pid]:
                interleaved.append(groups[pid].pop(0))
    return aged + interleaved


def admit_ready_tasks(session: Session, *, actor: str = "system:scheduler", limit: int | None = None) -> list[str]:
    """Admit READY LIVE tasks up to the concurrency cap in fair order (``fair_order``). Returns leased run ids."""
    leased: list[str] = []
    ready = fair_order(session, list(session.scalars(select(TaskExecution).where(
        TaskExecution.state == S.READY.value, TaskExecution.mode == DataMode.LIVE.value))))
    for te in ready:
        if limit is not None and len(leased) >= limit:
            break
        adm = admission_check(session, te.task_id)
        if not adm.admitted:
            # READY has no edge to BLOCKED in §14: the task keeps waiting with an explicit reason
            # that the UI turns into WAITING_QUOTA / BLOCKED (derived, canonical state stays READY).
            code = (adm.blocks or adm.waits)[0][0]
            _set_wait_reason(session, get_te(session, te.task_id), code, "; ".join(adm.reasons))
            continue
        try:
            run = acquire_lease(session, te.task_id, actor=actor, admission=adm)
        except Blocked as exc:
            # The reservation was refused by the cost module (§22 "Sistem mencadangkan compute, biaya,
            # dan slot sebelum memulai"): no lease, no run; the task waits with the cost module's reason.
            code = {"BUDGET_UNAVAILABLE": "MODULE_UNAVAILABLE", "QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED"}.get(
                exc.code, "BUDGET_EXCEEDED")
            reasons = "; ".join(str(r) for r in (exc.details or {}).get("reasons", [])[:5])
            _set_wait_reason(session, get_te(session, te.task_id), code,
                             f"{exc.code}: {exc.message}" + (f" ({reasons})" if reasons else ""))
            continue
        except Conflict:
            continue
        leased.append(run.id)
    return leased


# ----------------------------------------------------------------------------- heartbeat & observed actions
def _stale(session: Session, run: WorkerRun, lease_id: str | None) -> str | None:
    lease = session.get(Lease, lease_id) if lease_id else None
    if lease is None or lease.run_id != run.id:
        return "unknown lease"
    if lease.state != "ACTIVE":
        return f"lease {lease.state}"
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == run.task_id)) if run.task_id else None
    if te is not None and te.active_run_id != run.id:
        return "run is no longer the task's active attempt"
    if run.execution_status in RUN_TERMINAL:
        return f"run is {run.execution_status}"
    return None


def _reject_stale(session: Session, run: WorkerRun, why: str, kind: str, payload: dict[str, Any]) -> None:
    _audit(session, f"worker:run:{run.id}", f"workforce.run.{kind}.stale", result="DENIED", target_type="run",
           target_id=run.id, project_id=run.project_id, domain=run.domain, reason=why)
    events.emit(session, source="scheduler", kind="run.stale_event", entity_type="run", entity_id=run.id,
                run_id=run.id, attempt=run.attempt, lease_id=run.lease_id, project_id=run.project_id,
                domain=run.domain, mode=run.mode, payload={"rejected": kind, "why": why, **payload})


def heartbeat(session: Session, run_id: str, *, lease_id: str | None) -> bool:
    """Extend the lease. Returns False (and audits) for a stale attempt/lease (§50)."""
    run = get_run(session, run_id)
    why = _stale(session, run, lease_id)
    if why:
        _reject_stale(session, run, why, "heartbeat", {})
        return False
    lease = session.get(Lease, lease_id)
    now = utcnow()
    lease.heartbeat_at = now
    lease.expires_at = now + timedelta(seconds=lease.ttl_seconds or DEFAULT_LEASE_TTL)
    run.last_heartbeat_at = now
    session.flush()
    events.emit(session, source="runner", kind="run.heartbeat", entity_type="run", entity_id=run.id, run_id=run.id,
                attempt=run.attempt, lease_id=lease.id, project_id=run.project_id, domain=run.domain, mode=run.mode,
                payload={"lease_expires_at": lease.expires_at.isoformat(),
                         "note": "liveness only - not progress or quality"})
    return True


def record_action(session: Session, run_id: str, *, lease_id: str | None, action: dict[str, Any]) -> bool:
    run = get_run(session, run_id)
    why = _stale(session, run, lease_id)
    if why:
        _reject_stale(session, run, why, "action", {"action_kind": action.get("kind")})
        return False
    clean = redact(action)
    if not action.get("is_narration") and action.get("kind") not in ("narration", "step"):
        # observed runner tool action: the source label follows this writer (models.workforce listener)
        run._runner_action_write = True  # type: ignore[attr-defined]
        try:
            run.current_action = str(clean.get("label", ""))[:500]
        finally:
            run._runner_action_write = False  # type: ignore[attr-defined]
    path = clean.get("path")
    if action.get("kind") == "edit_file" and path and clean.get("status") == "completed":
        run.touched_paths = list(dict.fromkeys([*(run.touched_paths or []), str(path)[:400]]))[:2000]
    session.flush()
    events.emit(session, source="runner", kind="run.action", entity_type="run", entity_id=run.id, run_id=run.id,
                attempt=run.attempt, lease_id=run.lease_id, project_id=run.project_id, domain=run.domain,
                mode=run.mode, payload=clean)
    return True


# ----------------------------------------------------------------------------- cancel / reassign / repair / accept
def _revoke(session: Session, run_id: str, reason: str) -> int:
    from aeos.broker import facade as broker

    return broker.revoke_grants(session, run_id=run_id, reason=reason)


def _kill(run_id: str) -> dict[str, Any]:
    from aeos.worker_runtime import facade as runtime

    try:
        return runtime.kill_run_processes(run_id)
    except Exception as exc:  # never hide a failed kill
        log.exception("kill_run_processes failed for %s", run_id)
        return {"run_id": run_id, "found": True, "killed": [], "remaining": ["UNKNOWN"], "error": type(exc).__name__}


def _end_lease(session: Session, run: WorkerRun, state: str, reason: str) -> None:
    if not run.lease_id:
        return
    lease = session.get(Lease, run.lease_id)
    if lease is not None and lease.state == "ACTIVE":
        lease.state = state
        lease.released_at = utcnow()
        lease.release_reason = reason[:500]
        session.flush()


def stop_run_resources(session: Session, run: WorkerRun, *, reason: str, lease_state: str) -> dict[str, Any]:
    """Revoke capabilities FIRST, then kill the process tree, end the lease, release the reservation."""
    revoked = _revoke(session, run.id, reason)
    kill = _kill(run.id)
    _end_lease(session, run, lease_state, reason)
    released = ext.release(session, run.budget_reservation_id)
    return {"grants_revoked": revoked, "kill": kill, "reservation": released}


def _side_effects(session: Session, run_id: str) -> list[dict[str, Any]]:
    from aeos.models.broker_ext import BrokerSideEffect

    return [{"kind": se.kind, "status": se.status, "target": se.target, "external_url": se.external_url}
            for se in session.scalars(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run_id))]


def cancel_run(session: Session, run_id: str, *, reason: str, actor: str) -> WorkerRun:
    """Revoke capabilities, kill the process tree/sandbox, release reservations, keep evidence.
    External side effects that already happened are NOT assumed undone (§35)."""
    run = get_run(session, run_id)
    if run.execution_status in RUN_TERMINAL:
        return run
    res = stop_run_resources(session, run, reason=f"cancelled: {reason}", lease_state="REVOKED")
    effects = _side_effects(session, run.id)
    still = [p for p in res["kill"].get("remaining", [])]
    note = f"cancelled by {actor}: {reason}"
    if still:
        note += f" | WARNING processes still alive: {still}"
    run.current_action = "cancelled"
    set_run_status(session, run, S.CANCELLED.value, reason=note, actor=actor, outcome_code="CANCELLED")
    if run.task_id:
        te = get_te(session, run.task_id)
        if te.active_run_id == run.id and te.state not in {s.value for s in TERMINAL_EXECUTION_STATES}:
            transition_task(session, run.task_id, S.CANCELLED.value, reason=note, actor=actor)
    _audit(session, actor, "workforce.run.cancel", target_type="run", target_id=run.id, project_id=run.project_id,
           domain=run.domain, reason=reason, details={**res, "external_side_effects_not_undone": effects})
    events.emit(session, source="scheduler", kind="run.action", entity_type="run", entity_id=run.id, run_id=run.id,
                attempt=run.attempt, project_id=run.project_id, domain=run.domain, mode=run.mode,
                payload={"kind": "cancel", "label": "Run dibatalkan (capability dicabut, proses dihentikan)",
                         "grants_revoked": res["grants_revoked"], "processes_killed": len(res["kill"].get("killed", [])),
                         "processes_remaining": still, "reservation": res["reservation"],
                         "external_side_effects_not_undone": effects})
    return run


def cancel_task(session: Session, task_id: str, *, reason: str, actor: str) -> TaskExecution:
    te = get_te(session, task_id)
    if te.active_run_id:
        run = session.get(WorkerRun, te.active_run_id)
        if run is not None and run.execution_status not in RUN_TERMINAL:
            cancel_run(session, run.id, reason=reason, actor=actor)
            return get_te(session, task_id)
    return transition_task(session, task_id, S.CANCELLED.value, reason=f"cancelled by {actor}: {reason}", actor=actor)


def _close_reviewed_run(session: Session, te: TaskExecution, *, review_record_id: str | None, actor: str,
                       findings: int) -> None:
    """The attempt whose result came back with findings is not accepted: it ends FAILED
    (``REVIEW_FINDINGS``) so it can never later be shown as waiting for review or be accepted."""
    if not te.active_run_id:
        return
    run = session.get(WorkerRun, te.active_run_id)
    if run is None or run.execution_status != S.REVIEW.value:
        return
    if review_record_id:
        run.review_record_id = review_record_id
    _end_lease(session, run, "RELEASED", "review findings")
    set_run_status(session, run, S.FAILED.value, reason=f"review returned {findings} finding(s); not accepted",
                   actor=actor, outcome_code="REVIEW_FINDINGS")


def _discard_blocked_attempt(session: Session, te: TaskExecution, *, actor: str, why: str) -> None:
    """The owner replaced a quarantined / review-blocked attempt with a new one: the old candidate ends
    FAILED (``DISCARDED_BY_OWNER``). Left in REVIEW it would still look like it waits for review and could
    still be reviewed, accepted and published although the task moved on (§35, §50)."""
    if not te.active_run_id:
        return
    run = session.get(WorkerRun, te.active_run_id)
    if run is None or run.execution_status != S.REVIEW.value:
        return
    _end_lease(session, run, "RELEASED", "discarded by owner retry")
    set_run_status(session, run, S.FAILED.value, reason=why[:1000], actor=actor, outcome_code="DISCARDED_BY_OWNER")


def request_repair(session: Session, task_id: str, *, findings: list[Any], review_record_id: str | None,
                   actor: str) -> TaskExecution:
    """Reviewer findings -> a new bounded attempt (READY) or BLOCKED/NEEDS_DECISION when attempts are
    used up. The repair is done by a new implementer attempt — never by the reviewer (§19, §22)."""
    te = get_te(session, task_id)
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task spec missing")
    if te.state != S.REVIEW.value:
        raise Conflict(f"repair requires REVIEW, task is {te.state}", code="NOT_IN_REVIEW")
    agent = te.assigned_agent_key or IMPLEMENTER_AGENT_KEY
    if agent in REVIEWER_AGENT_KEYS:
        agent = IMPLEMENTER_AGENT_KEY
    clean = redact(list(findings or []))[:100]
    cap = max_attempts(task, te)
    _close_reviewed_run(session, te, review_record_id=review_record_id, actor=actor, findings=len(clean))
    if te.attempts_used < cap:
        return transition_task(session, task_id, S.READY.value,
                               reason=f"repair attempt {te.attempts_used + 1}/{cap} requested ({len(clean)} findings)",
                               actor=actor, pending_findings=clean, review_record_id=review_record_id,
                               active_run_id=None, assigned_agent_key=agent)
    return transition_task(session, task_id, S.BLOCKED.value,
                           reason=f"attempt cap reached ({te.attempts_used}/{cap}); owner decision required",
                           actor=actor, reason_code="NEEDS_DECISION", pending_findings=clean,
                           review_record_id=review_record_id)


def mark_accepted(session: Session, task_id: str, run_id: str, *, acceptance_id: str, actor: str) -> TaskExecution:
    """The ONLY path to DONE: REVIEW + an ACCEPTED AcceptanceRecord for this task/run that the
    verification module still considers valid for the run's base and the plan version hash."""
    te = get_te(session, task_id)
    task = session.get(TaskSpec, task_id)
    run = get_run(session, run_id)
    if task is None or run.task_id != task_id:
        raise Conflict("run does not belong to task", code="RUN_TASK_MISMATCH")
    if te.state != S.REVIEW.value:
        raise Conflict(f"task is {te.state}; DONE requires REVIEW", code="NOT_IN_REVIEW")
    if te.active_run_id not in (None, run_id):
        raise Conflict("acceptance must target the task's current attempt", code="STALE_ATTEMPT")
    rec = session.get(AcceptanceRecord, acceptance_id)
    if rec is None or rec.task_id != task_id or rec.run_id != run_id:
        raise Conflict("acceptance record does not match task/run", code="ACCEPTANCE_MISMATCH")
    if rec.acceptance_status != "ACCEPTED":
        raise Conflict(f"acceptance record is {rec.acceptance_status}", code="ACCEPTANCE_NOT_ACCEPTED")
    # §14 "Perubahan head/base yang relevan membatalkan acceptance": the record must be about the
    # exact candidate the runtime committed for this attempt.
    if rec.head_commit and run.head_commit and rec.head_commit != run.head_commit:
        raise Conflict("acceptance record was made for another head commit", code="ACCEPTANCE_HEAD_MISMATCH")
    if rec.base_commit and run.base_commit and rec.base_commit != run.base_commit:
        raise Conflict("acceptance record was made for another base commit", code="ACCEPTANCE_BASE_MISMATCH")
    if run.quarantined and ext.quarantine_release(session, run) is None:
        raise Blocked("patch is quarantined: special review and an owner release decision bound to the head "
                      "commit are required before acceptance (master s21)", code="QUARANTINE_REVIEW_REQUIRED",
                      details={"reasons": [str(r) for r in (run.quarantine_reasons or [])[:20]]})
    ok, why = ext.acceptance_valid_for_run(session, task, run.base_commit)
    if not ok:
        raise Blocked(f"acceptance not valid: {why}", code="ACCEPTANCE_INVALID")
    te = transition_task(session, task_id, S.DONE.value, reason=f"accepted ({acceptance_id})", actor=actor,
                         _allow_done=True, accepted_run_id=run_id)
    run.acceptance_status = "ACCEPTED"
    run.acceptance_id = acceptance_id
    if run.execution_status == S.REVIEW.value:
        set_run_status(session, run, S.DONE.value, reason="acceptance valid", actor=actor, outcome_code="ACCEPTED")
    _end_lease(session, run, "RELEASED", "accepted")
    return te


def reopen_for_reverification(session: Session, task_id: str, *, reason: str, actor: str) -> TaskExecution:
    """A DONE task whose acceptance became STALE (§14 "Perubahan head/base yang relevan membatalkan
    acceptance ... dan memicu verifikasi ulang", §50 "PR baru mengubah head atau base terkait").

    ``DONE -> READY`` (a new bounded attempt whose head/base is verified and reviewed again) while
    attempts remain and the plan version is still the approved one; otherwise ``DONE -> BLOCKED``
    (owner decision / new approval needed). Both carry ``ACCEPTANCE_STALE``. The accepted run keeps
    its history (acceptance_status STALE); nothing external is undone. Idempotent: a task that is not
    DONE is returned unchanged. Never commits."""
    te = get_te(session, task_id)
    if te.state != S.DONE.value:
        return te
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task spec missing")
    note = redact_text(reason)[:1500] or "acceptance became STALE"
    accepted = session.get(WorkerRun, te.accepted_run_id) if te.accepted_run_id else None
    if accepted is not None and accepted.acceptance_status != "STALE":
        accepted.acceptance_status = "STALE"
        session.flush()
    approved, why = ext.approved_version(session, te.plan_id)
    cap = max_attempts(task, te)
    findings = [{"source": "acceptance", "severity": "HIGH", "title": f"acceptance STALE: {note}"[:300],
                 "required_action": "re-verify and review the new head/base state"}]
    if approved is None or approved.id != task.plan_version_id:
        target, extra = S.BLOCKED.value, f"; plan version no longer approved ({why or 'superseded'})"
    elif te.attempts_used >= cap:
        target, extra = S.BLOCKED.value, (f"; attempt cap reached ({te.attempts_used}/{cap}): an owner decision "
                                           "(extra attempt) is required for the re-verification attempt")
    else:
        target, extra = S.READY.value, f"; re-verification attempt {te.attempts_used + 1}/{cap}"
    # the re-verification attempt starts from the changed head/base: the pinned base is released explicitly
    # (and audited below) instead of silently moving an in-flight task to a newer commit
    te = transition_task(session, task_id, target, reason=f"reopened: {note}{extra}", actor=actor,
                         reason_code="ACCEPTANCE_STALE", _allow_reopen=True, active_run_id=None,
                         pending_findings=findings, base_commit=None)
    _audit(session, actor, "workforce.task.reopen", target_type="task", target_id=task_id,
           project_id=te.project_id, domain=te.domain, reason=note,
           details={"to": target, "accepted_run_id": te.accepted_run_id, "attempts_used": te.attempts_used,
                    "base_commit": "released for re-pin at the next lease",
                    "max_attempts": cap})
    return te


def release_quarantine(session: Session, run_id: str, *, actor: str, actor_user_id: str, rationale: str) -> Decision:
    """Record the human owner's special-review release of a quarantined patch (§21). The decision is
    bound to this run and its exact runtime head commit; any other patch needs a new decision.
    This is human AUTH recorded through the policy module, never produced by a model."""
    run = get_run(session, run_id)
    if not run.quarantined:
        raise Conflict("run is not quarantined", code="NOT_QUARANTINED")
    if run.execution_status not in (S.REVIEW.value, S.DONE.value):
        raise Conflict(f"run is {run.execution_status}; only a REVIEW candidate can be released",
                       code="NOT_RELEASABLE")
    if not run.head_commit:
        raise Blocked("no runtime-recorded head commit to bind the decision to", code="NO_RUNTIME_HEAD")
    if not rationale.strip():
        raise ValidationFailed("a rationale referencing the special review is required")
    if not actor_user_id:
        raise ValidationFailed("a human owner must record the release")
    d = ext.record_decision(session, kind="EXCEPTION", actor_user_id=actor_user_id,
                            target_type=ext.QUARANTINE_TARGET_TYPE, target_id=run.id, target_hash=run.head_commit,
                            allowed_operations=["accept", "push_branch", "create_pr"], project_id=run.project_id,
                            domain=run.domain, rationale=rationale,
                            details={"quarantine_reasons": list(run.quarantine_reasons or [])[:50],
                                     "base_commit": run.base_commit})
    _audit(session, actor, "workforce.run.quarantine_release", target_type="run", target_id=run.id,
           project_id=run.project_id, domain=run.domain, reason=rationale,
           details={"decision_id": d.id, "head_commit": run.head_commit})
    events.emit(session, source="scheduler", kind="run.updated", entity_type="run", entity_id=run.id, run_id=run.id,
                attempt=run.attempt, project_id=run.project_id, domain=run.domain, mode=run.mode,
                payload={"quarantine_release_decision": d.id, "head_commit": run.head_commit})
    return d


def retry_task(session: Session, task_id: str, *, actor: str, extra_attempts: int = 0, rationale: str = "",
               actor_user_id: str | None = None) -> TaskExecution:
    """Operator retry (§35). REVIEW -> repair path; BLOCKED -> re-evaluate; attempt-cap blocks need
    an owner EXCEPTION decision (recorded through the policy module) — never silent."""
    te = get_te(session, task_id)
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task spec missing")
    if te.state == S.REVIEW.value:
        return request_repair(session, task_id, findings=list(te.pending_findings or []),
                              review_record_id=te.review_record_id, actor=actor)
    if te.state != S.BLOCKED.value:
        raise Conflict(f"nothing to retry: task is {te.state}"
                       + ("; terminal states need a new plan version" if te.state in
                          {s.value for s in TERMINAL_EXECUTION_STATES} else ""), code="NOT_RETRYABLE")
    if te.attempts_used >= max_attempts(task, te):
        if extra_attempts < 1 or not rationale.strip() or not actor_user_id:
            raise Blocked("attempt cap reached: an owner decision (extra_attempts + rationale) is required",
                          code="NEEDS_DECISION", details={"reasons": [te.reason]})
        d = ext.record_decision(session, kind="EXCEPTION", actor_user_id=actor_user_id, target_type="task",
                                target_id=task_id, target_hash=task.spec_hash, allowed_operations=["retry"],
                                project_id=task.project_id, domain=task.domain, rationale=rationale,
                                details={"extra_attempts": int(extra_attempts)})
        te.extra_attempts = (te.extra_attempts or 0) + int(min(extra_attempts, 3))
        te.extra_attempts_decision_id = d.id
        session.flush()
    if te.reason_code in HUMAN_BLOCK_CODES:
        # The owner explicitly asks for a new attempt instead of the pending special review / decision.
        _audit(session, actor, "workforce.task.retry_clears_block", target_type="task", target_id=task_id,
               project_id=te.project_id, domain=te.domain, reason=rationale, details={"code": te.reason_code})
        code = te.reason_code
        te.reason_code = ""
        session.flush()
        _discard_blocked_attempt(session, te, actor=actor, why=f"owner retry instead of {code} review: {rationale}")
    refresh_readiness(session, task.plan_version_id, actor=actor)
    te = get_te(session, task_id)
    if te.state != S.READY.value:
        raise Blocked("task is still blocked: " + te.reason, code="STILL_BLOCKED", details={"reasons": [te.reason]})
    return te


def reassign_task(session: Session, task_id: str, *, agent_key: str, actor: str, reason: str = "") -> TaskExecution:
    """Stop the old lease and revoke its capability BEFORE a new lease can be taken (§35)."""
    agent = session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    if agent is None:
        raise NotFound("agent definition not found")
    if agent.harness != "opencode" or agent_key in REVIEWER_AGENT_KEYS:
        raise ValidationFailed("only implementation agents can take a task (reviewers never repair)")
    if not agent.enabled:
        raise Blocked(f"agent {agent_key} is disabled", code="AGENT_DISABLED", details={"reasons": [agent.enabled_reason]})
    te = get_te(session, task_id)
    if te.state in {s.value for s in TERMINAL_EXECUTION_STATES} or te.state == S.REVIEW.value:
        raise Conflict(f"cannot reassign a task in {te.state}", code="NOT_REASSIGNABLE")
    note = f"reassigned to {agent_key} by {actor}: {reason}"
    if te.state in (S.LEASED.value, S.RUNNING.value) and te.active_run_id:
        run = get_run(session, te.active_run_id)
        res = stop_run_resources(session, run, reason=note, lease_state="REVOKED")
        set_run_status(session, run, S.CANCELLED.value, reason=note, actor=actor, outcome_code="REASSIGNED")
        te = transition_task(session, task_id, S.BLOCKED.value, reason=note, actor=actor, reason_code="REASSIGNED",
                             assigned_agent_key=agent_key)
        if res["kill"].get("remaining"):
            return te  # old process still alive: no new lease until it is gone
        return transition_task(session, task_id, S.READY.value, reason=note, actor=actor, active_run_id=None)
    te.assigned_agent_key = agent_key
    session.flush()
    _audit(session, actor, "workforce.task.reassign", target_type="task", target_id=task_id,
           project_id=te.project_id, domain=te.domain, reason=reason, details={"agent_key": agent_key})
    return te


# ----------------------------------------------------------------------------- reconciliation (§14, §50)
def reconcile(session: Session, *, actor: str = "system:reconciler") -> dict[str, Any]:
    """After a crash/restart or lease expiry: read leases/reservations/runs and check whether the old
    process is alive BEFORE any retry. Never starts a duplicate while an old worker may be active."""
    from aeos.worker_runtime import processes

    now = utcnow()
    summary: dict[str, Any] = {"expired": 0, "alive": 0, "orphans_resolved": 0}
    expired = [lz for lz in session.scalars(select(Lease).where(Lease.state == "ACTIVE"))
               if ensure_aware(lz.expires_at) <= now]
    for lease in expired:
        run = session.get(WorkerRun, lease.run_id)
        if run is None:
            lease.state = "EXPIRED"
            continue
        summary["expired"] += 1
        _revoke(session, run.id, "lease expired")
        alive = processes.is_alive(run.pid, run.pid_create_time)
        kill: dict[str, Any] = {}
        if alive:
            kill = _kill(run.id)
            alive = processes.is_alive(run.pid, run.pid_create_time)
        lease.state = "EXPIRED"
        lease.released_at = now
        lease.release_reason = "expired without heartbeat"
        ext.release(session, run.budget_reservation_id)
        code = "LEASE_LOST_PROCESS_ALIVE" if alive else "LEASE_LOST"
        if alive:
            summary["alive"] += 1
            run.outcome_code = "ORPHANED_ALIVE"
            run.block_reason = f"lease expired; worker pid {run.pid} still alive after kill attempt"
        elif run.execution_status not in RUN_TERMINAL:
            set_run_status(session, run, S.FAILED.value, reason="lease expired; worker process not alive",
                           actor=actor, outcome_code="LEASE_LOST")
        te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == run.task_id)) if run.task_id else None
        if te is not None and te.active_run_id == run.id and te.state in (S.LEASED.value, S.RUNNING.value):
            transition_task(session, te.task_id, S.BLOCKED.value, actor=actor, reason_code=code,
                            reason=f"lease expired ({code}); kill={kill.get('methods', [])} - no duplicate started")
        _audit(session, actor, "workforce.lease.expired", target_type="run", target_id=run.id,
               project_id=run.project_id, domain=run.domain, details={"alive": alive, "kill": kill})
    for run in session.scalars(select(WorkerRun).where(WorkerRun.outcome_code == "ORPHANED_ALIVE")):
        if not processes.is_alive(run.pid, run.pid_create_time) and run.execution_status not in RUN_TERMINAL:
            set_run_status(session, run, S.FAILED.value, reason="orphaned worker process ended", actor=actor,
                           outcome_code="LEASE_LOST")
            summary["orphans_resolved"] += 1
    session.flush()
    return summary


def _authorisation_lost(session: Session, run: WorkerRun, te: TaskExecution) -> str | None:
    """Why a running attempt is no longer authorised (None = still authorised)."""
    from aeos.worker_runtime.sandbox import sandbox_readiness

    approved, why = ext.approved_version(session, te.plan_id)
    if approved is None or approved.id != run.plan_version_id:
        return "plan approval no longer ACTIVE for this version" + (f" ({why})" if why else "")
    sb = sandbox_readiness(session, domain=run.domain)
    if sb["status"] != "READY":
        return "sandbox requirement no longer met: " + "; ".join(sb.get("reasons", []))[:300]
    if run.domain == "OFFICE":
        ok, owhy = ext.office_external_allowed(session)
        if not ok:
            return owhy
    try:
        from aeos.proposals import facade as proposals

        lost = proposals.trial_authorisation_lost(session, run.task_id)
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - proposals ships with the foundation
        task = session.get(TaskSpec, run.task_id) if run.task_id else None
        lost = "proposal trial binding cannot be verified (fail closed)" if _is_trial_task(task) else None
    if lost:
        return f"proposal trial authority lost: {lost}"[:500]
    return None


def enforce_stop_conditions(session: Session, *, actor: str = "system:scheduler") -> dict[str, Any]:
    """§22 "Stop berlaku saat ... policy berubah": a LEASED/RUNNING LIVE attempt whose authorisation
    no longer holds (plan approval invalidated/superseded, sandbox exception revoked or expired,
    OFFICE policy) is stopped — capability revoked first, process tree killed, lease revoked,
    reservation released — and the task waits BLOCKED (``POLICY_CHANGED``) instead of finishing
    under a policy that no longer applies. Evidence is kept. Missing modules fail closed."""
    stopped: list[str] = []
    runs = list(session.scalars(select(WorkerRun).where(
        WorkerRun.mode == DataMode.LIVE.value, WorkerRun.task_id.is_not(None),
        WorkerRun.execution_status.in_([S.LEASED.value, S.RUNNING.value]))))
    for run in runs:
        te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == run.task_id))
        if te is None or te.active_run_id != run.id:
            continue
        why = _authorisation_lost(session, run, te)
        if not why:
            continue
        note = f"stopped: {why}"
        res = stop_run_resources(session, run, reason=note[:120], lease_state="REVOKED")
        run.current_action = "Dihentikan: kebijakan/otorisasi berubah"
        set_run_status(session, run, S.FAILED.value, reason=note, actor=actor, outcome_code="POLICY_CHANGED")
        if te.state in (S.LEASED.value, S.RUNNING.value):
            transition_task(session, te.task_id, S.BLOCKED.value, reason=note, actor=actor,
                            reason_code="POLICY_CHANGED", expected_from=te.state)
        _audit(session, actor, "workforce.run.policy_stop", target_type="run", target_id=run.id,
               project_id=run.project_id, domain=run.domain, reason=why,
               details={"grants_revoked": res["grants_revoked"], "remaining": res["kill"].get("remaining", [])})
        stopped.append(run.id)
    session.flush()
    return {"stopped": stopped}


# ----------------------------------------------------------------------------- plan execution
def execute_plan(session: Session, plan_id: str, *, actor: str, user_id: str | None) -> dict[str, Any]:
    from aeos.models.planning import Plan

    plan = session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    approved, why = ext.approved_version(session, plan_id)
    if approved is None:
        raise Blocked(f"plan cannot execute: {why}", code="PLAN_NOT_APPROVED", details={"reasons": [why]})
    ok, reasons = ext.capability_ready(session, Capability.CAN_RUN_WORKER, domain=plan.domain, project_id=plan.project_id)
    if not ok:
        raise Blocked("CAN_RUN_WORKER is not READY", code="CAPABILITY_NOT_READY", details={"reasons": reasons})
    tes = ensure_executions(session, approved.id, actor=actor, requested_by=user_id)
    counts = refresh_readiness(session, approved.id, actor=actor)
    _audit(session, actor, "workforce.plan.execute", target_type="plan", target_id=plan_id, project_id=plan.project_id,
           domain=plan.domain, details={"plan_version_id": approved.id, **counts})
    return {"plan_id": plan_id, "plan_version_id": approved.id, "tasks": len(tes), **counts,
            "scheduler_enabled": scheduler_enabled(session), "paused": scheduling_paused(session),
            "note": "READY tasks are admitted by the scheduler loop (or `aeos work run-once`)"}


def counts_by_state(session: Session, *, domain: str, project_ids: list[str] | None = None) -> dict[str, int]:
    q = select(TaskExecution.state, func.count()).where(TaskExecution.domain == domain,
                                                        TaskExecution.mode == DataMode.LIVE.value)
    if project_ids is not None:
        q = q.where(TaskExecution.project_id.in_(project_ids))
    out = {s.value: 0 for s in S}
    for state, n in session.execute(q.group_by(TaskExecution.state)):
        out[state] = int(n)
    return out


__all__ = [
    "CONTROLLER_ID",
    "RUNNING_STOP_CODES",
    "Admission",
    "acquire_lease",
    "admission_check",
    "admit_ready_tasks",
    "cancel_run",
    "cancel_task",
    "concurrency_cap",
    "enforce_stop_conditions",
    "ensure_executions",
    "execute_plan",
    "heartbeat",
    "mark_accepted",
    "reassign_task",
    "reconcile",
    "record_action",
    "refresh_readiness",
    "release_quarantine",
    "reopen_for_reverification",
    "request_repair",
    "retry_task",
    "route_quota_exhausted",
    "scheduling_paused",
    "scope_contract_problems",
    "set_scheduling_paused",
    "transition_task",
    "update_run",
]
