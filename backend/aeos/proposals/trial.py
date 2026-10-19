"""Trial execution under the TRIAL decision (§40 "Isolated trial dan candidate patch", "Trial admission").

* ``launch_trial`` is the owner's one action that turns an ACTIVE, still-bound TRIAL decision into the
  canonical execution flow: a Plan + immutable PlanVersion in the proposal's project (the platform project
  for PLATFORM scope) whose single TaskSpec carries the proposal id, decision id, candidate pin/hash, the
  decision's caps (time/attempts/budget/data class), stop conditions and the trial operations. The plan
  version is approved in the same owner action, bound to its exact manifest hash, with the TRIAL decision
  as its authority (§40 "Satu authorization dapat mencakup rangkaian operasi trial ... tidak perlu bertanya
  untuk setiap API dalam scope yang sah"). Enqueueing goes through the workforce scheduler; when a
  capability (worker/sandbox) is not READY the trial waits and says why - nothing is bypassed.
* The scheduler re-checks the binding before every attempt (``admission_problems``) and while it runs
  (``authorisation_lost``): stale hashes, a stopped trial, an operation outside the decision, a provider
  other than the decided one, or exhausted caps refuse/stop the attempt.
* ``scope_drift`` compares what actually ran (task data class, run provider, spend, attempts) with the
  decision scope; any drift invalidates the decision (§40 "Jika hash, provider, data class, permission,
  budget atau target material berubah, authority terkait harus dinilai ulang").
* ``stop_trial`` is the owner's stop/kill: running attempts are cancelled (capabilities revoked, process tree
  killed, reservations released, evidence kept).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.planning import TaskSpec
from aeos.models.research import ChangeProposal

ARTIFACT_PREFIX = "change_proposal:"
TRIAL_OPERATIONS = ["trial.sandbox", "trial.task.run", "trial.eval.run", "trial.verifier.collect", "trial.stop"]
TASK_OPERATIONS = ["trial.task.run", "trial.verifier.collect"]
CLASS_ORDER = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
TRIAL_PLAN_SOURCE = "CHANGE_PROPOSAL"


def _service() -> Any:
    from aeos.proposals import service

    return service


def artifact_label(a: Any) -> str:
    """The label of a TaskSpec input artifact: the plan's string, or the ``label`` the task compiler keeps on the
    hash-bound artifact dict (planning.dag.resolve_input_artifacts)."""
    if isinstance(a, dict):
        return str(a.get("label") or "")
    return a if isinstance(a, str) else ""


def proposal_id_for_task(task: TaskSpec | None) -> str | None:
    for a in (task.input_artifacts if task is not None else None) or []:
        label = artifact_label(a)
        if label.startswith(ARTIFACT_PREFIX):
            return label[len(ARTIFACT_PREFIX):]
    return None


def trial_task_ids(p: ChangeProposal) -> list[str]:
    return list((p.trial or {}).get("task_ids") or [])


# --------------------------------------------------------------------------- scope vs what actually ran


def scope_drift(session: Session, p: ChangeProposal, decision: Any) -> list[str]:
    """Material scope that actually ran outside the TRIAL decision (data class, domain, provider, spend,
    attempts). Only records of tasks linked to this proposal are read."""
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.routing import ModelRoute
    from aeos.models.workforce import WorkerRun

    task_ids = list(p.task_ids or [])
    if not task_ids:
        return []
    scope = decision.auth_scope or {}
    problems: list[str] = []
    dc = str(scope.get("data_class") or "").upper()
    for t in session.scalars(select(TaskSpec).where(TaskSpec.id.in_(task_ids))):
        if t.domain != p.domain:
            problems.append(f"task {t.task_key} is in domain {t.domain}, not {p.domain}")
        if dc in CLASS_ORDER and t.data_classification in CLASS_ORDER and \
                CLASS_ORDER.index(t.data_classification) > CLASS_ORDER.index(dc):
            problems.append(f"task {t.task_key} data class {t.data_classification} exceeds the decided {dc}")
    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.task_id.in_(task_ids),
                                                        WorkerRun.mode == "LIVE")))
    provider = scope.get("provider")
    per_task: dict[str, int] = {}
    for r in runs:
        per_task[r.task_id or ""] = per_task.get(r.task_id or "", 0) + 1
        if r.domain != p.domain:
            problems.append(f"run {r.id[:8]} ran in domain {r.domain}")
        if provider and r.model_route_key:
            route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == r.model_route_key))
            if route is not None and route.provider != provider:
                problems.append(f"run {r.id[:8]} used provider {route.provider}, outside the decided provider {provider}")
    cap = scope.get("max_attempts")
    if isinstance(cap, int) and cap > 0:
        over = [t for t, n in per_task.items() if n > cap]
        if over:
            problems.append(f"{len(over)} task(s) used more than the decided {cap} attempts")
    budget = scope.get("budget_usd")
    if isinstance(budget, (int, float)) and runs:
        spent = session.scalar(select(func.coalesce(func.sum(UsageLedgerEntry.cost_usd), 0)).where(
            UsageLedgerEntry.run_id.in_([r.id for r in runs]), UsageLedgerEntry.mode == "LIVE"))
        if float(spent or 0) > float(budget) + 1e-9:
            problems.append(f"trial spend {float(spent):.4f} USD exceeds the decided budget {float(budget):.4f} USD")
    return problems


# --------------------------------------------------------------------------- scheduler hooks


def admission_problems(session: Session, task: TaskSpec, *, route: Any = None, running: bool = False) -> list[tuple[str, str]]:
    """Blocks for an attempt of a proposal-bound TaskSpec (empty for any other task)."""
    pid = proposal_id_for_task(task)
    if pid is None:
        return []
    service = _service()
    p = session.get(ChangeProposal, pid)
    if p is None or p.domain != task.domain:
        return [("TRIAL_NOT_AUTHORISED", f"change proposal {pid} not found in domain {task.domain}")]
    trial = p.trial or {}
    if trial.get("status") == "STOPPED":
        return [("TRIAL_STOPPED", f"trial of proposal {pid} was stopped by the owner")]
    if p.status not in service.TRIAL_STATUSES:
        return [("TRIAL_NOT_AUTHORISED", f"proposal {pid} is {p.status}; no trial authority")]
    d, problems = service.binding_problems(session, p, action="TRIAL_APPROVE")
    if d is None:
        return [("TRIAL_NOT_AUTHORISED", f"no ACTIVE TRIAL decision for proposal {pid}")]
    out: list[tuple[str, str]] = []
    if problems:
        service._invalidate(session, p, "binding stale at trial admission: " + "; ".join(problems)[:300])
        p.status = "UNDER_REVIEW"
        session.flush()
        return [("STALE_BINDING", "; ".join(problems)[:500])]
    scope = d.auth_scope or {}
    ops = set((task.capability_scope or {}).get("operations") or [])
    if not ops <= set(d.allowed_operations or []):
        out.append(("TRIAL_OPERATION_NOT_AUTHORISED",
                    f"operations {sorted(ops - set(d.allowed_operations or []))} are outside the TRIAL decision"))
    if route is not None and scope.get("provider") and getattr(route, "provider", None) != scope["provider"]:
        out.append(("TRIAL_SCOPE_PROVIDER", f"selected route {route.route_key} is provider {route.provider}; the "
                                            f"TRIAL decision allows {scope['provider']} only (no other provider)"))
    from aeos.models.workforce import TaskExecution

    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    cap = scope.get("max_attempts")
    if te is not None and isinstance(cap, int) and (te.attempts_used > cap if running else te.attempts_used >= cap):
        out.append(("TRIAL_ATTEMPTS_EXHAUSTED", f"{te.attempts_used}/{cap} trial attempts used"))
    minutes = scope.get("max_minutes")
    launched = trial.get("launched_at")
    if isinstance(minutes, int) and launched:
        from datetime import datetime

        start = ensure_aware(datetime.fromisoformat(launched))
        if start is not None and utcnow() > start + timedelta(minutes=minutes):
            out.append(("TRIAL_TIME_EXHAUSTED", f"trial time cap of {minutes} minutes since launch reached"))
    return out


def authorisation_lost(session: Session, task_id: str | None) -> str | None:
    """Why a running proposal-bound attempt must stop (None = still authorised or not proposal-bound)."""
    task = session.get(TaskSpec, task_id) if task_id else None
    if proposal_id_for_task(task) is None:
        return None
    problems = admission_problems(session, task, running=True)  # type: ignore[arg-type]
    return "; ".join(f"{c}: {m}" for c, m in problems) or None


# --------------------------------------------------------------------------- launch / stop


def _trial_project(session: Session, p: ChangeProposal) -> Any:
    from aeos.models.portfolio import Project

    if p.project_id:
        proj = session.get(Project, p.project_id)
    else:
        from aeos.portfolio import facade as portfolio

        proj = portfolio.platform_project(session)
    if proj is None or proj.domain != p.domain:
        raise Blocked("no project in this domain can host the trial (PLATFORM scope uses the DLY AEOS platform project, "
                      "which is PERSONAL); scope the proposal to a project of this domain",
                      code="TRIAL_PROJECT_UNAVAILABLE")
    if (proj.lifecycle or "ACTIVE").upper() in ("ARCHIVED", "RETIRED"):
        raise Blocked(f"project lifecycle is {proj.lifecycle}", code="TRIAL_PROJECT_UNAVAILABLE")
    return proj


def _safe_paths(paths: list[str] | None, p: ChangeProposal) -> list[str]:
    from aeos.planning.dag import unsafe_path

    out = [str(x).strip() for x in (paths or []) if str(x).strip()]
    for x in out:
        why = unsafe_path(x)
        if why:
            raise ValidationFailed(f"allowed path {x!r} is not a safe repository-relative path: {why}")
    return out or [f"sandbox/trials/{p.id[:8]}/**"]


def _plan_content(p: ChangeProposal, d: Any, allowed: list[str]) -> dict[str, Any]:
    from aeos.proposals import review as R

    scope = d.auth_scope or {}
    det = d.details or {}
    suites = [s["suite_key"] for s in ((p.review_package or {}).get("experiment_plan") or {}).get("suites") or []
              if isinstance(s, dict) and s.get("available")]
    criteria = [f"{a['id']}: {a['criterion']} (metrik {a['metric']}; ambang {a['threshold']}; baseline "
                f"{a.get('baseline_value') or 'UNKNOWN'})" for a in R.measurable_acceptance(p)]
    env = scope.get("environment")
    task = {
        "key": "trial-candidate",
        "title": redact_text(f"Trial {p.kind} {p.candidate_pin}: {p.title}")[:200],
        "goal": redact_text(
            f"Uji terisolasi kandidat {p.kind} (pin {p.candidate_pin}) terhadap baseline pada environment {env}. "
            "Hasilnya hanya bukti trial untuk keputusan PROMOTION terpisah; tidak ada promosi, merge, aktivasi, "
            "perubahan route/policy atau kenaikan budget."),
        "role_key": "feature-implementer",
        "input_artifacts": [f"{ARTIFACT_PREFIX}{p.id}", f"trial_decision:{d.id}", f"candidate_pin:{p.candidate_pin}",
                            f"candidate_hash:{det.get('candidate_hash')}", f"baseline_hash:{det.get('baseline_hash')}"],
        "expected_outputs": ["Patch adapter/config kandidat pada candidate branch",
                             "Laporan verifier independen yang memetakan acceptance -> bukti"],
        "acceptance_criteria": criteria,
        "allowed_paths": allowed,
        "limits": {"max_attempts": scope.get("max_attempts"), "timeout_seconds": int(scope.get("max_minutes") or 0) * 60},
        "capability_scope": {"operations": list(TASK_OPERATIONS),
                             "notes": f"Sandbox sekali pakai; environment={env}; rollback scope: "
                                      f"{scope.get('rollback_scope') or 'UNKNOWN'}"},
        "data_classification": scope.get("data_class"),
        "stop_conditions": list(scope.get("stop_conditions") or []),
        "verifier_policy": {"suites": suites or ["unit", "integration", "acceptance"], "negative_tests_required": True},
        "model_route_policy": {"pool": "WORKER", "notes": f"Provider dibatasi keputusan TRIAL: {scope.get('provider')}"},
        "risk_class": "R2",
    }
    return {
        "title": redact_text(f"Trial: {p.title}")[:256],
        "summary": "Rencana trial deterministik dari ChangeProposal dan keputusan TRIAL yang terikat hash (§40).",
        "source": {"change_proposal_id": p.id, "trial_decision_id": d.id, "binding_hash": d.target_hash,
                   "baseline_hash": det.get("baseline_hash"), "candidate_hash": det.get("candidate_hash"),
                   "candidate_pin": p.candidate_pin, "scope": scope},
        "milestones": [], "features": [], "tasks": [task],
    }


def launch_trial(session: Session, p: ChangeProposal, *, actor_user_id: str,
                 allowed_paths: list[str] | None = None) -> dict[str, Any]:
    service = _service()
    if p.parent_id:
        raise Conflict("launch the trial on the parent proposal")
    if p.status not in ("TRIAL_APPROVED", "IN_TRIAL"):
        raise Conflict(f"a trial is launched after TRIAL_APPROVE (current {p.status})")
    if (p.trial or {}).get("status") == "LAUNCHED":
        raise Conflict("the trial is already launched; stop it before launching again", code="TRIAL_ALREADY_LAUNCHED")
    d, problems = service.binding_problems(session, p, action="TRIAL_APPROVE")
    if d is None or problems:
        n = service._invalidate(session, p, "binding stale at trial launch: " + "; ".join(problems)[:300]) if d else 0
        p.status = "UNDER_REVIEW" if d else p.status
        session.flush()
        raise service.StaleBinding("the TRIAL decision is not bound to the live baseline/candidate/scope; re-review",
                                   details={"problems": problems or ["no ACTIVE TRIAL decision"],
                                            "invalidated_decisions": n})
    project = _trial_project(session, p)
    allowed = _safe_paths(allowed_paths, p)
    from aeos.planning import dag
    from aeos.planning import service as planning
    from aeos.planning.states import transition

    scope = d.auth_scope or {}
    prompt = (f"Trial terisolasi untuk ChangeProposal {p.id} ({p.kind}) di bawah keputusan TRIAL {d.id}. Kandidat "
              f"{p.candidate_pin}; environment {scope.get('environment')}; batas {scope.get('max_minutes')} menit, "
              f"{scope.get('max_attempts')} attempt, budget {scope.get('budget_usd')} USD, kelas data "
              f"{scope.get('data_class')}. Tidak ada promosi tanpa keputusan PROMOTION terpisah.")
    plan = planning.create_plan(session, project=project, domain=p.domain, title=f"Trial: {p.title}"[:256],
                                prompt=prompt, attachment_ids=[], actor_user_id=actor_user_id)
    version = planning.create_version(session, plan, _plan_content(p, d, allowed), source=TRIAL_PLAN_SOURCE,
                                      parent_version_id=None, run_ids=[], created_by=actor_user_id)
    plan.policy_baseline = {**(plan.policy_baseline or {}), "trial_plan": {
        "source": "ChangeProposal TRIAL decision (§40)", "change_proposal_id": p.id, "trial_decision_id": d.id,
        "planning_pipeline": "NOT_APPLICABLE: deterministic trial plan derived from a hash-bound owner TRIAL decision"}}
    transition(session, plan, "NEEDS_DECISION", actor=actor_user_id,
               reason="Rencana trial deterministik dari ChangeProposal; persetujuan terikat hash versi oleh pemilik.")
    transition(session, plan, "READY_FOR_HUMAN_REVIEW", actor=actor_user_id,
               reason=f"Versi {version.version} siap; otoritas: keputusan TRIAL {d.id}.")
    approval = planning.decide(session, plan.id, action="APPROVE", version_id=version.id,
                               version_hash=version.manifest_hash, targets=[], actor_user_id=actor_user_id,
                               rationale=f"Trial ChangeProposal {p.id} dalam scope keputusan TRIAL {d.id} "
                                         f"(binding {d.target_hash}); bukan promosi.")
    tasks = dag.compiled_tasks(session, version.id)
    execution: dict[str, Any]
    try:
        from aeos.workforce import scheduler

        out = scheduler.execute_plan(session, plan.id, actor=f"user:{actor_user_id}", user_id=actor_user_id)
        execution = {"status": "ENQUEUED", **{k: out.get(k) for k in ("tasks", "ready", "blocked", "scheduler_enabled",
                                                                      "paused", "note")}}
    except Blocked as exc:
        execution = {"status": "WAITING", "code": exc.code,
                     "reasons": [str(r) for r in (exc.details or {}).get("reasons") or [exc.message]][:10],
                     "note": "Trial menunggu kesiapan worker/sandbox; jalankan dari halaman Plan setelah READY "
                             "(tidak ada jalan pintas)."}
    now = utcnow().isoformat()
    task_ids = [t.id for t in tasks]
    p.task_ids = sorted({*(p.task_ids or []), *task_ids})
    links = dict(p.links or {})
    links["plan_version_ids"] = sorted({*(links.get("plan_version_ids") or []), version.id})
    p.links = links
    p.trial = {
        "status": "LAUNCHED", "launched_at": now, "launched_by": actor_user_id, "decision_id": d.id,
        "binding_hash": d.target_hash, "plan_id": plan.id, "plan_version_id": version.id,
        "plan_version_hash": version.manifest_hash, "plan_approval_decision_id": approval["decision"]["id"],
        "task_ids": task_ids, "project_id": project.id, "allowed_paths": allowed,
        "environment": scope.get("environment"), "caps": {k: scope.get(k) for k in
                                                          ("max_minutes", "max_attempts", "budget_usd", "data_class",
                                                           "provider")},
        "operations": list(d.allowed_operations or []), "execution": execution,
        "history": [*((p.trial or {}).get("history") or []), {"at": now, "event": "LAUNCHED", "by": actor_user_id,
                                                              "plan_id": plan.id}],
    }
    p.status = "IN_TRIAL"
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.trial.launch",
                 target_type=service.TARGET_TYPE, target_id=p.id, project_id=project.id, domain=p.domain,
                 details={"decision_id": d.id, "plan_id": plan.id, "plan_version_id": version.id, "task_ids": task_ids,
                          "execution": execution["status"], "installed_in_baseline": False})
    events.emit(session, source="proposals", kind="proposal.trial.launched", entity_type=service.TARGET_TYPE,
                entity_id=p.id, project_id=project.id, domain=p.domain,
                payload={"plan_id": plan.id, "task_ids": task_ids, "execution": execution["status"]})
    return dict(p.trial)


def stop_trial(session: Session, p: ChangeProposal, *, actor_user_id: str, reason: str) -> dict[str, Any]:
    service = _service()
    trial = dict(p.trial or {})
    if trial.get("status") != "LAUNCHED":
        raise Conflict("no running trial to stop", code="TRIAL_NOT_LAUNCHED")
    if len((reason or "").strip()) < 3:
        raise ValidationFailed("a stop needs a reason")
    from aeos.workforce import scheduler

    cancelled: list[dict[str, Any]] = []
    for tid in trial.get("task_ids") or []:
        try:
            te = scheduler.cancel_task(session, tid, reason=redact_text(reason)[:200], actor=f"user:{actor_user_id}")
            cancelled.append({"task_id": tid, "state": te.state})
        except (Conflict, NotFound) as exc:
            cancelled.append({"task_id": tid, "state": "UNCHANGED", "reason": exc.code})
    now = utcnow().isoformat()
    trial.update({"status": "STOPPED", "stopped_at": now, "stopped_by": actor_user_id,
                  "stop_reason": redact_text(reason)[:500], "cancelled": cancelled,
                  "history": [*(trial.get("history") or []), {"at": now, "event": "STOPPED", "by": actor_user_id}]})
    p.trial = trial
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.trial.stop",
                 target_type=service.TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain,
                 reason=reason, details={"cancelled": cancelled})
    events.emit(session, source="proposals", kind="proposal.trial.stopped", entity_type=service.TARGET_TYPE,
                entity_id=p.id, project_id=p.project_id, domain=p.domain, payload={"cancelled": len(cancelled)})
    return trial
