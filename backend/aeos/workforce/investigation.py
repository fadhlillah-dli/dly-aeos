"""§19 "Beberapa hipotesis bug -> Read-only investigations terpisah dengan timebox; integrator memilih perbaikan
berbukti" (run type INVESTIGATION).

TaskSpec pattern (written in the plan content, so it is part of the human-approved PlanVersion; no new schema field:
``capability_scope.operations`` carries it):

    "operations": ["investigation", "investigation.timebox=600", "investigation.repair=fix-lease",
                   "hypothesis:h1=Lease renewal races with reconcile", "hypothesis:h2=Heartbeat uses local clock"]

* each hypothesis is investigated by a SEPARATE WorkerRun (``run_type=INVESTIGATION``), one after another under the
  pilot's one-active-worker cap (an investigation never fans out; every run passes admission, budget and sandbox);
* the worker runs OpenCode READ-ONLY (read/glob/grep/list; no edit/write/patch/bash/web), with the short timebox,
  and returns a findings + evidence-refs report only - never a candidate commit (any change is a violation);
* when every hypothesis has been investigated the task waits in REVIEW (``INVESTIGATION_COMPLETE``) for the integrator:
  the owner compares the reports and records the chosen EVIDENCED fix (verdict SUPPORTED with at least one evidence
  reference the runtime found in the repository) as a human APPROVAL decision bound to the compared reports. The
  chosen finding becomes the input of the plan's ONE repair IMPLEMENTATION TaskSpec (``investigation.repair``), which
  depends on the investigation and stays waiting until that decision exists.

Worker reports are untrusted DATA: a verdict is never acceptance, and nothing here runs a model. Services never commit.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.broker import facade as broker_contract
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.planning import TaskSpec
from aeos.models.workforce import TaskExecution, WorkerRun

RUN_TYPE = "INVESTIGATION"
MARKER = "investigation"
MAX_HYPOTHESES = 5
DEFAULT_TIMEBOX = 900
MIN_TIMEBOX, MAX_TIMEBOX = 60, 3600
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TARGET_TYPE = "investigation"
CHOICE_KIND = "APPROVAL"
CHOICE_OPERATION = "open_repair_task"
# A hypothesis counts as investigated when its run ended for a reason about the hypothesis itself; a run stopped by
# quota/lease/shutdown/cancel did not investigate anything (the owner may grant another attempt).
INVESTIGATED_OUTCOMES = frozenset({"INVESTIGATION_REPORTED", "INVESTIGATION_REPORT_INVALID", "TIMEOUT", "EXIT_NONZERO",
                                   "SCOPE_VIOLATION", "RESOURCE_LIMIT"})
# Only a run that ended cleanly with a valid report can carry an evidenced finding: a report from a run that wrote a
# file, timed out or exited non-zero is inconclusive, whatever it claims.
REPORTED_OUTCOME = "INVESTIGATION_REPORTED"
# The hypothesis outcome is recorded once, when its run ends (``caps.investigation_outcome``). ``outcome_code`` is the
# run's lifecycle code and changes later (integrator choice, review findings); it never decides what was investigated.
OUTCOME_CAP = "investigation_outcome"
READ_ONLY_FORBIDDEN_TOOLS = ("bash", "webfetch", "edit", "write", "patch")
# ADR-0127: the broker refused a model request of the run (it carried a credential). Not an investigated outcome: the
# hypothesis is not counted as investigated and the task is BLOCKED instead of moving to the next hypothesis.
SENSITIVE_STOP = broker_contract.SENSITIVE_CONTENT_BLOCKED


# ----------------------------------------------------------------------------- the TaskSpec pattern
def spec_of(task: Any) -> dict[str, Any] | None:
    """The investigation declared by *task* (None when it is not an investigation TaskSpec)."""
    ops = [str(o) for o in ((getattr(task, "capability_scope", None) or {}).get("operations") or [])]
    if MARKER not in ops:
        return None
    hypotheses: list[dict[str, str]] = []
    timebox: Any = DEFAULT_TIMEBOX
    repair: str | None = None
    for op in ops:
        if op.startswith("hypothesis:"):
            key, _, statement = op[len("hypothesis:"):].partition("=")
            hypotheses.append({"key": key.strip(), "statement": redact_text(statement.strip())[:1000]})
        elif op.startswith("investigation.timebox="):
            timebox = op.split("=", 1)[1].strip()
        elif op.startswith("investigation.repair="):
            repair = op.split("=", 1)[1].strip() or None
    try:
        timebox_s: int | None = int(timebox)
    except (TypeError, ValueError):
        timebox_s = None
    return {"hypotheses": hypotheses, "timebox_seconds": timebox_s, "repair_task_key": repair}


def spec_problems(session: Session, task: TaskSpec, spec: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    hyps = spec["hypotheses"]
    if not 1 <= len(hyps) <= MAX_HYPOTHESES:
        problems.append(f"an investigation names 1..{MAX_HYPOTHESES} hypotheses (got {len(hyps)})")
    keys = [h["key"] for h in hyps]
    if len(set(keys)) != len(keys):
        problems.append("hypothesis keys must be unique")
    for h in hyps:
        if not KEY_RE.match(h["key"]):
            problems.append(f"hypothesis key {h['key']!r} must match {KEY_RE.pattern}")
        if len(h["statement"]) < 5:
            problems.append(f"hypothesis {h['key']} needs a statement")
    tb = spec["timebox_seconds"]
    if tb is None or not MIN_TIMEBOX <= tb <= MAX_TIMEBOX:
        problems.append(f"investigation.timebox must be {MIN_TIMEBOX}..{MAX_TIMEBOX} seconds")
    asked = {str(t).split(":", 1)[0].lower() for t in (task.tools_allowed or [])}
    if asked & set(READ_ONLY_FORBIDDEN_TOOLS) or task.egress_allowed:
        problems.append("a read-only investigation never gets edit/write/patch/bash/web tools or egress")
    repair_key = spec["repair_task_key"]
    if not repair_key:
        problems.append("investigation.repair=<task_key> must name the plan's repair IMPLEMENTATION task")
    else:
        repair = repair_task(session, task, repair_key)
        if repair is None:
            problems.append(f"repair task {repair_key} is not in this plan version")
        elif task.task_key not in (repair.depends_on or []):
            problems.append(f"repair task {repair_key} must depend on investigation {task.task_key}")
        elif spec_of(repair) is not None:
            problems.append("the repair task must be an IMPLEMENTATION task, not another investigation")
    return problems


def repair_task(session: Session, task: TaskSpec, key: str) -> TaskSpec | None:
    return session.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == task.plan_version_id,
                                                 TaskSpec.task_key == key))


def investigation_runs(session: Session, task_id: str) -> list[WorkerRun]:
    return list(session.scalars(select(WorkerRun).where(WorkerRun.task_id == task_id, WorkerRun.run_type == RUN_TYPE)
                                .order_by(WorkerRun.attempt, WorkerRun.created_at)))


def _hyp_key(run: WorkerRun) -> str | None:
    return (((run.caps or {}).get("investigation") or {}).get("hypothesis") or {}).get("key")


def _outcome(run: WorkerRun) -> str:
    """The hypothesis outcome recorded when the run ended (stable), else the run's code (a run still in flight or
    stopped before ``after_run``, e.g. QUOTA_EXHAUSTED, which investigated nothing)."""
    return str((run.caps or {}).get(OUTCOME_CAP) or run.outcome_code or "")


def hypotheses_state(session: Session, task: TaskSpec, spec: dict[str, Any]) -> list[dict[str, Any]]:
    runs = investigation_runs(session, task.id)
    out = []
    for i, h in enumerate(spec["hypotheses"], start=1):
        mine = [r for r in runs if _hyp_key(r) == h["key"]]
        done = [r for r in mine if _outcome(r) in INVESTIGATED_OUTCOMES]
        last = done[-1] if done else (mine[-1] if mine else None)
        report = ((last.caps or {}).get("investigation_report") or {}) if last is not None else {}
        outcome = _outcome(last) if last is not None else None
        out.append({"key": h["key"], "statement": h["statement"], "index": i, "runs": [r.id for r in mine],
                    "investigated": bool(done), "run_id": last.id if last is not None else None,
                    "outcome_code": outcome, "run_outcome_code": last.outcome_code if last is not None else None,
                    "verdict": report.get("verdict"), "valid": bool(report.get("valid")),
                    # evidenced = a clean, valid report (no write, timeout or non-zero exit) whose SUPPORTED verdict
                    # names at least one reference the runtime found in the repository
                    "evidenced": bool(report.get("evidenced")) and outcome == REPORTED_OUTCOME,
                    "evidence_refs": report.get("evidence_checked") or [],
                    "summary": report.get("summary"), "proposed_fix": report.get("proposed_fix"),
                    "findings": report.get("findings") or [], "report_evidence_ref": report.get("evidence_ref")})
    return out


def next_hypothesis(session: Session, task: TaskSpec, spec: dict[str, Any]) -> dict[str, Any] | None:
    for h in hypotheses_state(session, task, spec):
        if not h["investigated"]:
            return h
    return None


def max_attempts(spec: dict[str, Any], extra_attempts: int) -> int:
    """One timeboxed run per hypothesis; a retry needs the owner's extra attempts (retry_task)."""
    return max(1, len(spec["hypotheses"])) + max(0, int(extra_attempts or 0))


def lease_caps(spec: dict[str, Any], hypothesis: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    timebox = int(spec["timebox_seconds"] or DEFAULT_TIMEBOX)
    return {"investigation": {"hypothesis": {"key": hypothesis["key"], "statement": hypothesis["statement"]},
                              "index": hypothesis["index"], "of": len(spec["hypotheses"]), "timebox_seconds": timebox,
                              "read_only": True, "repair_task_key": spec["repair_task_key"]},
            "timeout_seconds": min(int(timeout_seconds), timebox)}


# ----------------------------------------------------------------------------- after one hypothesis run
def after_run(session: Session, run: WorkerRun, te: TaskExecution, task: TaskSpec, *, exit_code: int | None,
              stop_reason: str | None, violations: list[dict[str, str]], actor: str) -> str:
    """Map one finished investigation run to §14 transitions (called by the runtime's outcome phase; resources are
    already released). Exit 0 with a valid report -> run REVIEW (data for the integrator, never DONE); anything else
    -> run FAILED with its code. The task returns to READY for the next hypothesis, or waits in REVIEW for the
    integrator's choice when every hypothesis has been investigated."""
    from aeos.core.enums import ExecutionState as S
    from aeos.workforce import scheduler

    report = (run.caps or {}).get("investigation_report") or {}
    if stop_reason in ("TIMEOUT", "RESOURCE_LIMIT", SENSITIVE_STOP):
        code = stop_reason
    elif violations or stop_reason == "SCOPE_VIOLATION":
        code = "SCOPE_VIOLATION"
    elif exit_code != 0 or stop_reason:
        code = "EXIT_NONZERO"
    elif not report.get("valid"):
        code = "INVESTIGATION_REPORT_INVALID"
    else:
        code = "INVESTIGATION_REPORTED"
    hyp = _hyp_key(run) or "?"
    run.verifier_status = "NOT_APPLICABLE"  # no candidate: the trusted suite has nothing to judge
    run.acceptance_status = "NOT_APPLICABLE"
    run.caps = {**(run.caps or {}), OUTCOME_CAP: code}  # recorded once; later lifecycle codes never change it
    if code == "INVESTIGATION_REPORTED":
        scheduler.set_run_status(session, run, S.REVIEW.value, actor=actor, outcome_code=code,
                                 reason=f"hypothesis {hyp}: read-only report recorded (untrusted DATA, verdict "
                                        f"{report.get('verdict')}); awaiting the integrator")
    else:
        scheduler.set_run_status(session, run, S.FAILED.value, actor=actor, outcome_code=code,
                                 reason=f"hypothesis {hyp}: {code} (inconclusive; no evidenced finding)")
    spec = spec_of(task) or {"hypotheses": []}
    if code == SENSITIVE_STOP:
        # ADR-0127: the broker refused a model request of this run because it carried a credential. The next hypothesis
        # would read the same content and be refused again, so nothing is queued: the task waits, BLOCKED, until the
        # owner removes the credential from what the worker reads (the hypothesis stays uninvestigated; retry_task).
        why = (f"hypothesis {hyp}: {SENSITIVE_STOP} - a model request carried a credential and was not sent (see the "
               "run's broker.model_proxy.sensitive_blocked audit record); remove the credential from what the worker "
               "reads, then retry")
        scheduler.transition_task(session, te.task_id, S.BLOCKED.value, actor=actor, expected_from=S.RUNNING.value,
                                  reason_code=SENSITIVE_STOP, reason=why)
        run.block_reason = why
        session.flush()
        return "BLOCKED"
    remaining = next_hypothesis(session, task, spec)
    attempts_left = te.attempts_used < max_attempts(spec, te.extra_attempts)
    if remaining is not None and attempts_left:
        scheduler.transition_task(session, te.task_id, S.REVIEW.value, actor=actor, expected_from=S.RUNNING.value,
                                  reason_code="INVESTIGATION_NEXT",
                                  reason=f"hypothesis {hyp} investigated ({code})")
        scheduler.transition_task(session, te.task_id, S.READY.value, actor=actor, expected_from=S.REVIEW.value,
                                  reason=f"next hypothesis {remaining['key']} ({remaining['index']}/"
                                         f"{len(spec['hypotheses'])}), one run at a time")
        return "NEXT_HYPOTHESIS"
    done = sum(1 for h in hypotheses_state(session, task, spec) if h["investigated"])
    note = (f"NEEDS_DECISION: {done}/{len(spec['hypotheses'])} hypotheses investigated; the integrator compares the "
            "reports and chooses the evidenced fix (not accepted)")
    scheduler.transition_task(session, te.task_id, S.REVIEW.value, actor=actor, expected_from=S.RUNNING.value,
                              reason_code="INVESTIGATION_COMPLETE", reason=note)
    run.block_reason = note  # surfaces the run in the decision queue (RUN_REVIEW, NEEDS_DECISION)
    session.flush()
    return "INVESTIGATION_COMPLETE"


# ----------------------------------------------------------------------------- the integrator step
def comparison(session: Session, task: TaskSpec) -> dict[str, Any]:
    spec = spec_of(task)
    if spec is None:
        raise ValidationFailed("task is not an investigation TaskSpec", code="NOT_AN_INVESTIGATION")
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    hyps = hypotheses_state(session, task, spec)
    material = [{k: h[k] for k in ("key", "run_id", "outcome_code", "verdict", "evidenced", "report_evidence_ref")}
                for h in hyps]
    choice = chosen_decision(session, task)
    return {"task_id": task.id, "task_key": task.task_key, "state": te.state if te else None,
            "reason_code": te.reason_code if te else None, "repair_task_key": spec["repair_task_key"],
            "timebox_seconds": spec["timebox_seconds"], "hypotheses": hyps,
            "evidenced": [h["key"] for h in hyps if h["evidenced"]],
            "complete": bool(te and te.state == "REVIEW" and te.reason_code in ("INVESTIGATION_COMPLETE",
                                                                                "INVESTIGATION_CHOSEN")),
            "comparison_hash": content_hash(material),
            "choice": ({"decision_id": choice.id, "hypothesis_key": (choice.details or {}).get("hypothesis_key")}
                       if choice is not None else None)}


def chosen_decision(session: Session, task: TaskSpec) -> Any:
    """The integrator's ACTIVE human choice recorded by ``choose_fix`` (its operation and chosen hypothesis), newest
    first. A decision on the same target without them (e.g. hand-recorded elsewhere) is not a choice."""
    from aeos.models.decisions import Decision

    q = (select(Decision).where(Decision.kind == CHOICE_KIND, Decision.target_type == TARGET_TYPE,
                                Decision.target_id == task.id, Decision.status == "ACTIVE",
                                Decision.actor_kind == "HUMAN")
         .order_by(Decision.created_at.desc()))
    for d in session.scalars(q):
        if CHOICE_OPERATION in (d.allowed_operations or []) and (d.details or {}).get("hypothesis_key"):
            return d
    return None


def choose_fix(session: Session, task_id: str, *, hypothesis_key: str, actor: str, actor_user_id: str,
               rationale: str) -> dict[str, Any]:
    """The integrator (a human owner) records the chosen evidenced fix; the repair TaskSpec then carries it."""
    from aeos.events import service as events
    from aeos.workforce import integrations as ext
    from aeos.workforce import scheduler

    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("a rationale (min 10 characters) is required", code="RATIONALE_REQUIRED")
    view = comparison(session, task)
    te = scheduler.get_te(session, task_id)
    if te.state != "REVIEW" or te.reason_code != "INVESTIGATION_COMPLETE":
        raise Conflict(f"investigation is {te.state}/{te.reason_code or '-'}: every hypothesis must be investigated "
                       "before the integrator chooses", code="INVESTIGATION_NOT_COMPLETE")
    chosen = next((h for h in view["hypotheses"] if h["key"] == hypothesis_key), None)
    if chosen is None:
        raise ValidationFailed(f"unknown hypothesis {hypothesis_key}")
    if not chosen["evidenced"]:
        raise Blocked(f"hypothesis {hypothesis_key} has no evidenced finding (verdict {chosen['verdict'] or 'none'}, "
                      f"outcome {chosen['outcome_code']}); only an evidenced fix opens the repair task",
                      code="INVESTIGATION_FIX_NOT_EVIDENCED", details={"evidenced": view["evidenced"]})
    repair = repair_task(session, task, view["repair_task_key"] or "")
    if repair is None:
        raise Blocked("the plan declares no repair task for this investigation", code="INVESTIGATION_REPAIR_MISSING")
    repair_te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == repair.id))
    if repair_te is None or repair_te.state not in ("QUEUED", "BLOCKED", "READY"):
        raise Conflict(f"repair task {repair.task_key} is {repair_te.state if repair_te else 'not queued'}; it must "
                       "wait for this choice", code="REPAIR_TASK_NOT_WAITING")
    d = ext.record_decision(
        session, kind=CHOICE_KIND, actor_user_id=actor_user_id, target_type=TARGET_TYPE, target_id=task.id,
        target_hash=view["comparison_hash"], allowed_operations=[CHOICE_OPERATION], project_id=task.project_id,
        domain=task.domain, rationale=redact_text(rationale)[:2000],
        auth_scope={"repair_task_id": repair.id, "repair_task_key": repair.task_key},
        details={"hypothesis_key": hypothesis_key, "run_id": chosen["run_id"], "verdict": chosen["verdict"],
                 "evidence_refs": chosen["evidence_refs"][:20], "report_evidence_ref": chosen["report_evidence_ref"],
                 "compared": [{k: h[k] for k in ("key", "verdict", "evidenced", "outcome_code", "run_id")}
                              for h in view["hypotheses"]],
                 "repair_task_id": repair.id, "untrusted_worker_data": True})
    finding = (f"[DATA from investigation {task.task_key}/{hypothesis_key}, chosen by the owner in decision {d.id}; "
               f"untrusted worker report, verify it] {redact_text(str(chosen.get('proposed_fix') or chosen.get('summary') or ''))[:1500]}"
               f" | evidence: {', '.join(str(e.get('ref')) for e in chosen['evidence_refs'][:10] if e.get('exists'))}")
    repair_te.pending_findings = [finding]
    for r in investigation_runs(session, task.id):
        if r.execution_status == "REVIEW":
            scheduler.set_run_status(session, r, "DONE", actor=actor,
                                     outcome_code="INVESTIGATION_CHOSEN" if _hyp_key(r) == hypothesis_key
                                     else "INVESTIGATION_NOT_CHOSEN",
                                     reason=f"integrator decision {d.id}: chose {hypothesis_key}")
            r.block_reason = ""
    te.reason_code = "INVESTIGATION_CHOSEN"
    te.reason = (f"integrator chose {hypothesis_key} (decision {d.id}); repair task {repair.task_key} carries the fix "
                 "(investigation itself is never accepted)")
    te.updated_at = utcnow()
    session.flush()
    scheduler._audit(session, actor, "workforce.investigation.choose", target_type="task", target_id=task.id,
                     project_id=task.project_id, domain=task.domain, reason=redact_text(rationale)[:500],
                     details={"decision_id": d.id, "hypothesis_key": hypothesis_key, "repair_task_id": repair.id})
    events.emit(session, source="controller", kind="task.investigation_chosen", entity_type="task", entity_id=task.id,
                project_id=task.project_id, domain=task.domain,
                payload={"decision_id": d.id, "hypothesis_key": hypothesis_key, "repair_task_key": repair.task_key})
    scheduler.refresh_readiness(session, task.plan_version_id, actor=actor)
    return {"task_id": task.id, "decision_id": d.id, "hypothesis_key": hypothesis_key, "repair_task_id": repair.id,
            "repair_task_key": repair.task_key, "comparison_hash": view["comparison_hash"]}


def dependency_satisfied(session: Session, dep: TaskSpec, dep_te: TaskExecution | None) -> tuple[bool, str] | None:
    """For a dependency that is an investigation: satisfied once the integrator's ACTIVE choice exists (an
    investigation is never accepted). None when *dep* is not an investigation (normal acceptance rule)."""
    if spec_of(dep) is None:
        return None
    choice = chosen_decision(session, dep)
    if dep_te is None or dep_te.reason_code != "INVESTIGATION_CHOSEN" or choice is None:
        return False, (f"investigation {dep.task_key} has no recorded integrator choice "
                       f"({dep_te.state if dep_te else 'not queued'}/{dep_te.reason_code if dep_te else '-'})")
    current = comparison(session, dep)["comparison_hash"]
    if choice.target_hash != current:  # hash-bound: the choice was made on other reports than the ones on record now
        return False, (f"investigation {dep.task_key}: the integrator choice {choice.id} was made on other reports "
                       "(comparison hash changed); a new choice is required")
    return True, ""
