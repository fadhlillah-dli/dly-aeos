"""Read-only projections for the Workforce Console (§35). No new authority or state lives here:
every row is computed from canonical TaskExecution/WorkerRun/Lease/Evidence/Decision records.

UI states WAITING_QUOTA / WAITING_REVIEW / STALE are *derived*; the canonical execution state is
always returned alongside. Worker-reported, verifier, review, acceptance and merged status are
separate fields — never one check mark.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import DataMode, ExecutionState
from aeos.core.timeutil import age_seconds, utcnow
from aeos.models.audit import Event
from aeos.models.broker_ext import BrokerRequest, BrokerSideEffect
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import AgentDefinition, Lease, RoleSkillPack, TaskExecution, WorkerRun
from aeos.workforce import scheduler
from aeos.workforce.catalog import GROUP_LABELS, GROUPS

S = ExecutionState
STALE_HEARTBEAT_SECONDS = 60
BLOCKING_WAIT_CODES = frozenset({"CAPABILITY_NOT_READY", "SANDBOX_NOT_READY", "ROUTE_INELIGIBLE", "AGENT_DISABLED",
                                 "OFFICE_DENIED", "PLAN_NOT_APPROVED", "DEPENDENCY_NOT_ACCEPTED", "COST_UNKNOWN",
                                 "MODULE_UNAVAILABLE", "FAN_OUT_FORBIDDEN", "NO_ALLOWED_PATHS", "NO_EXPECTED_OUTPUT",
                                 "ROLE_UNKNOWN", "ROLE_NOT_EVALUATED", "ROLE_RESULT_NOT_ADOPTED", "AGENT_OUT_OF_SCOPE",
                                 "CONTEXT_TOO_LARGE",
                                 "UNSAFE_PATH", "FORBIDDEN_PATH_ALLOWED", "VERIFIER_UNAVAILABLE",
                                 "OFFICE_ROUTE_NOT_AUTHORISED",
                                 # §41: only a new approved plan version with the sections filled clears these
                                 "DOCS_NOT_READY", "DOCS_READINESS_UNVERIFIED"})
CURRENT_ACTION_LABELS = {"RUNNER": "observed tool action (runner)", "CONTROLLER": "controller status (bukan aksi tool)"}


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def derive_ui_state(run: WorkerRun | None, te: TaskExecution | None) -> str:
    """UI-only state derived from the canonical state + reason (§35). A run's own outcome always
    wins: a FAILED/CANCELLED attempt is never shown as "waiting for review" just because its task
    went to REVIEW for a repair decision, and another attempt's task state is never borrowed."""
    if run is not None and run.execution_status in (S.RUNNING.value, S.LEASED.value):
        age = age_seconds(run.last_heartbeat_at or run.started_at or run.created_at)
        if age is not None and age > STALE_HEARTBEAT_SECONDS:
            return "STALE"
        if run.quota_status == "RATE_LIMITED":
            return "WAITING_QUOTA"
        return run.execution_status
    if run is not None and run.execution_status == S.REVIEW.value:
        if te is not None and te.active_run_id == run.id and te.state == S.BLOCKED.value:
            # quarantined / review-blocked candidate: it waits for the owner, not for a review (§35, §50
            # "Worker selesai tetapi verifier gagal -> REVIEW/BLOCKED sesuai state")
            return "BLOCKED"
        # §50 "Fable Max quota habis -> Review menunggu": the checkpoint review waits for quota
        return "WAITING_QUOTA" if run.quota_status == "WAITING_QUOTA" else "WAITING_REVIEW"
    if run is not None and (run.execution_status in (S.DONE.value, S.FAILED.value, S.CANCELLED.value)
                            or te is None or te.active_run_id != run.id):
        return run.execution_status
    if te is not None:
        if te.reason_code in scheduler.QUOTA_CODES:
            return "WAITING_QUOTA"
        if te.state == S.REVIEW.value:
            return "WAITING_REVIEW"
        if te.state == S.READY.value and te.reason_code in BLOCKING_WAIT_CODES:
            return "BLOCKED"
        return te.state
    return run.execution_status if run is not None else "UNKNOWN"


def _owner_requirements(run: WorkerRun, te: TaskExecution | None) -> list[str]:
    req = ["Merge ke branch utama hanya oleh manusia (U-09)"]
    if run.execution_status == S.REVIEW.value or (te and te.state == S.REVIEW.value):
        req.append("Menunggu verifier independen dan review checkpoint sebelum acceptance")
    if run.quarantined:
        req.append("Patch dikarantina: review khusus diperlukan sebelum promosi")
    if te is not None and te.reason_code == "NEEDS_DECISION":
        req.append("Batas attempt tercapai: keputusan pemilik diperlukan")
    if run.caps and run.caps.get("sandbox_decision_id"):
        req.append("Berjalan dengan pengecualian process sandbox (lihat keputusan)")
    return req


def _grant_info(session: Session, run: WorkerRun) -> dict[str, Any] | None:
    g = session.get(CapabilityGrant, run.capability_id) if run.capability_id else None
    if g is None:
        return None
    from aeos.broker.grants import grant_view

    v = grant_view(g)
    return {"active": v["active"], "expires_at": v["expires_at"], "used": v["used"], "caps": v["caps"],
            "revoked_reason": v["revoked_reason"]}


def run_row(session: Session, run: WorkerRun, *, projects: dict[str, Project] | None = None,
            roles: dict[str, RoleSkillPack] | None = None) -> dict[str, Any]:
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == run.task_id)) if run.task_id else None
    task = session.get(TaskSpec, run.task_id) if run.task_id else None
    project = (projects or {}).get(run.project_id) or session.get(Project, run.project_id)
    role = (roles or {}).get(run.role_key or "") or (
        session.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == run.role_key)) if run.role_key else None)
    hb_age = age_seconds(run.last_heartbeat_at)
    active = run.execution_status in (S.RUNNING.value, S.LEASED.value)
    usage = dict(run.usage_summary or {})
    cost = usage.get("cost_usd")
    return {
        "run_id": run.id, "run_type": run.run_type, "attempt": run.attempt, "mode": run.mode,
        "project": {"id": run.project_id, "name": project.name if project else ("DEMO" if run.mode != "LIVE" else None),
                    "key": project.project_key if project else None},
        "domain": run.domain,
        "agent_key": run.agent_key, "role_key": run.role_key, "role_name": role.name if role else run.role_key,
        "task": {"id": run.task_id, "key": run.task_key or (task.task_key if task else None),
                 "title": task.title if task else None, "feature_key": task.feature_key if task else None},
        "route": {"key": run.model_route_key, "model_resolved": run.model_resolved or "UNKNOWN",
                  "engine": run.engine, "engine_version": run.engine_version or "UNKNOWN"},
        "execution_state": run.execution_status,
        "task_state": te.state if te else None,
        "ui_state": derive_ui_state(run, te),
        "wait_reason": run.block_reason or (te.reason if te and te.active_run_id == run.id else ""),
        "reason_code": run.outcome_code or (te.reason_code if te else ""),
        "current_action": run.current_action,
        # §35: the label follows the writer (runner tool action vs controller status text), never assumed
        "current_action_source": CURRENT_ACTION_LABELS.get(run.current_action_source or "",
                                                           "UNKNOWN (sumber tidak tercatat)"),
        "current_action_writer": run.current_action_source or "UNKNOWN",
        # §35 Live Work "files/resources touched": external resources the broker touched for this run
        "resources": [{"kind": se.kind, "target": se.target, "status": se.status, "external_url": se.external_url}
                      for se in session.scalars(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id)
                                                .order_by(BrokerSideEffect.created_at).limit(25))],
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "last_heartbeat_at": _iso(run.last_heartbeat_at),
        "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "heartbeat_stale": bool(active and (hb_age is None or hb_age > STALE_HEARTBEAT_SECONDS)),
        "touched_paths": list(run.touched_paths or [])[:25], "touched_count": len(run.touched_paths or []),
        "statuses": {"worker_reported": run.worker_reported_status, "verifier": run.verifier_status,
                     "review": run.review_verdict, "acceptance": run.acceptance_status, "merged": run.merged_status},
        "usage": {"requests": usage.get("requests"), "prompt_tokens": usage.get("prompt_tokens"),
                  "completion_tokens": usage.get("completion_tokens"),
                  "cost_usd": cost if cost is not None and not usage.get("cost_unknown_requests") else
                  ("UNKNOWN" if usage.get("requests") else None),
                  "cost_known_usd": cost, "cost_unknown_requests": usage.get("cost_unknown_requests", 0),
                  "quota_status": run.quota_status or "UNKNOWN", "ledger": usage.get("ledger")},
        "caps": run.caps or {},
        "capability": _grant_info(session, run),
        "evidence_refs": list(run.evidence_refs or []),
        "quarantined": bool(run.quarantined),
        "exit_code": run.exit_code,
        "owner_requirements": _owner_requirements(run, te),
        "links": {"run": f"/runs/{run.id}"},
    }


def list_runs(session: Session, *, domain: str, project_ids: list[str] | None, active_only: bool = False,
              limit: int = 200, include_demo: bool = True) -> list[dict[str, Any]]:
    q = select(WorkerRun).where(WorkerRun.domain == domain)
    if project_ids is not None:
        q = q.where(WorkerRun.project_id.in_(project_ids))
    if not include_demo:
        q = q.where(WorkerRun.mode == DataMode.LIVE.value)
    if active_only:
        q = q.where(WorkerRun.execution_status.in_([S.LEASED.value, S.RUNNING.value]))
    q = q.order_by(WorkerRun.created_at.desc()).limit(min(limit, 500))
    runs = list(session.scalars(q))
    projects = {p.id: p for p in session.scalars(select(Project).where(Project.id.in_({r.project_id for r in runs})))}
    roles = {r.role_key: r for r in session.scalars(select(RoleSkillPack))}
    return [run_row(session, r, projects=projects, roles=roles) for r in runs]


def _events_for_run(session: Session, run_id: str, limit: int = 400) -> list[dict[str, Any]]:
    rows = session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.seq.asc()).limit(limit))
    out = []
    for e in rows:
        payload = e.payload or {}
        narration = payload.get("kind") == "narration" or bool(payload.get("is_narration"))
        out.append({"seq": e.seq, "at": _iso(e.at), "kind": e.kind, "source": e.source, "attempt": e.attempt,
                    "lease_id": e.lease_id, "mode": e.mode, "payload": payload,
                    "label": "summary (narasi AI, bukan bukti)" if narration else "observed"})
    return out


def run_detail(session: Session, run: WorkerRun) -> dict[str, Any]:
    row = run_row(session, run)
    task = session.get(TaskSpec, run.task_id) if run.task_id else None
    te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == run.task_id)) if run.task_id else None
    lease = session.get(Lease, run.lease_id) if run.lease_id else None
    attempts = []
    if run.task_id:
        for r in session.scalars(select(WorkerRun).where(WorkerRun.task_id == run.task_id).order_by(WorkerRun.attempt)):
            attempts.append({"run_id": r.id, "attempt": r.attempt, "status": r.execution_status,
                             "outcome_code": r.outcome_code, "agent_key": r.agent_key, "exit_code": r.exit_code,
                             "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
                             "quarantined": bool(r.quarantined), "current": r.id == run.id})
    evidence = [{"id": e.id, "kind": e.kind, "origin": e.origin, "trusted": e.trusted, "level": e.evidence_level,
                 "claim": e.claim, "content_hash": e.content_hash, "created_at": _iso(e.created_at), "mode": e.mode}
                for e in session.scalars(select(EvidenceArtifact).where(EvidenceArtifact.run_id == run.id)
                                         .order_by(EvidenceArtifact.created_at))]
    reviews = [{"id": r.id, "verdict": r.verdict, "reviewer_kind": r.reviewer_kind, "findings": r.findings,
                "unknowns": r.unknowns, "created_at": _iso(r.created_at), "mode": r.mode}
               for r in session.scalars(select(ReviewRecord).where(
                   or_(ReviewRecord.id == (run.review_record_id or ""),
                       (ReviewRecord.target_type == "run") & (ReviewRecord.target_id == run.id))))]
    acceptance = [{"id": a.id, "status": a.acceptance_status, "review_verdict": a.review_verdict,
                   "base_commit": a.base_commit, "head_commit": a.head_commit, "invalidated_reason": a.invalidated_reason,
                   "created_at": _iso(a.created_at)}
                  for a in session.scalars(select(AcceptanceRecord).where(AcceptanceRecord.run_id == run.id))]
    targets = [run.id] + ([run.task_id] if run.task_id else []) + ([task.plan_version_id] if task else [])
    sandbox_decision = (run.caps or {}).get("sandbox_decision_id")
    decisions = [{"id": d.id, "kind": d.kind, "target_type": d.target_type, "target_id": d.target_id,
                  "target_hash": d.target_hash, "status": d.status, "rationale": d.rationale,
                  "created_at": _iso(d.created_at)}
                 for d in session.scalars(select(Decision).where(
                     or_(Decision.target_id.in_(targets), Decision.id == (sandbox_decision or ""))))]
    side_effects = [{"id": se.id, "kind": se.kind, "status": se.status, "target": se.target,
                     "external_url": se.external_url, "commit_sha": se.commit_sha, "updated_at": _iso(se.updated_at),
                     "note": "efek eksternal tidak dibatalkan otomatis oleh cancel"}
                    for se in session.scalars(select(BrokerSideEffect).where(BrokerSideEffect.run_id == run.id))]
    calls = [{"at": _iso(c.created_at), "route_key": c.route_key, "outcome": c.outcome, "status": c.status_code,
              "prompt_tokens": c.prompt_tokens, "completion_tokens": c.completion_tokens,
              "cost_usd": float(c.cost_usd) if c.cost_usd is not None else "UNKNOWN",
              "model_resolved": c.model_resolved, "latency_ms": c.latency_ms}
             for c in session.scalars(select(BrokerRequest).where(BrokerRequest.run_id == run.id)
                                      .order_by(BrokerRequest.created_at).limit(200))]
    spec = None
    if task is not None:
        spec = {
            "task_key": task.task_key, "title": task.title, "goal": task.goal, "feature_key": task.feature_key,
            "acceptance_criteria": task.acceptance_criteria, "expected_outputs": task.expected_outputs,
            "allowed_paths": task.allowed_paths, "forbidden_paths": task.forbidden_paths,
            "tools_allowed": task.tools_allowed, "egress_allowed": task.egress_allowed, "limits": task.limits,
            "stop_conditions": task.stop_conditions, "evidence_requirements": task.evidence_requirements,
            "risk_class": task.risk_class, "data_classification": task.data_classification,
            "depends_on": task.depends_on, "spec_hash": task.spec_hash, "plan_version_id": task.plan_version_id,
            "plan_version_hash": task.plan_version_hash, "base_commit": task.base_commit,
            "capability_ttl_seconds": task.capability_ttl_seconds,
        }
    return {
        **row,
        "task_spec": spec,
        "task_execution": None if te is None else {
            "state": te.state, "reason": te.reason, "reason_code": te.reason_code, "attempts_used": te.attempts_used,
            "max_attempts": scheduler.max_attempts(task, te) if task else None, "row_version": te.row_version,
            "pending_findings": te.pending_findings, "assigned_agent_key": te.assigned_agent_key},
        "base_commit": run.base_commit, "head_commit": run.head_commit, "branch": run.branch,
        "workspace": {"sandbox_kind": run.sandbox_kind, "strategy": (run.diff_summary or {}).get("strategy"),
                      "present": bool(run.workspace_ref)},
        "lease": None if lease is None else {"id": lease.id, "state": lease.state, "owner": lease.owner,
                                             "expires_at": _iso(lease.expires_at), "heartbeat_at": _iso(lease.heartbeat_at),
                                             "release_reason": lease.release_reason},
        "diff_summary": run.diff_summary or {},
        "quarantine_reasons": run.quarantine_reasons or [],
        "actions": _events_for_run(session, run.id),
        "attempts": attempts, "evidence": evidence, "reviews": reviews, "acceptance": acceptance,
        "decisions": decisions, "side_effects": side_effects, "model_calls": calls,
        "error": run.error, "block_reason": run.block_reason,
    }


def _active_counts(session: Session, column: Any, *, domain: str, mode: str,
                   project_ids: list[str] | None = None) -> dict[str, int]:
    """Active run counts, scoped like every other read: domain AND the principal's project scope
    (§50 'Viewer mengakses ... Office: backend scope diterapkan termasuk counts')."""
    q = select(column, func.count()).where(WorkerRun.domain == domain, WorkerRun.mode == mode,
                                           WorkerRun.execution_status.in_([S.LEASED.value, S.RUNNING.value]))
    if project_ids is not None:
        q = q.where(WorkerRun.project_id.in_(project_ids))
    return {k: int(n) for k, n in session.execute(q.group_by(column)) if k}


PLANNING_STAGE_ROLES = {"PRIMARY_PLANNING": "primary-planner", "CHALLENGE_REVIEW": "planning-challenger",
                        "SYNTHESIS": "planning-synthesizer", "FINAL_REVIEW": "final-planning-reviewer"}
REVIEWER_KIND_ROLES = {"CLAUDE": "feature-architecture-reviewer", "SECURITY": "security-reviewer"}


def _supervision_counts(session: Session, *, domain: str, project_ids: list[str] | None) -> dict[str, int]:
    """§17 "slot yang punya run aktif": planning stages that are RUNNING or wait for the manual package, and
    checkpoint/security reviews that are open (waiting for quota or the manual package), domain/project scoped."""
    from aeos.models.planning import Plan, PlanningRun

    out: dict[str, int] = {}
    q = (select(PlanningRun.stage, func.count()).join(Plan, Plan.id == PlanningRun.plan_id)
         .where(Plan.domain == domain, PlanningRun.status.in_(["RUNNING", "AWAITING_MANUAL"])))
    if project_ids is not None:
        q = q.where(PlanningRun.project_id.in_(project_ids))
    for stage, n in session.execute(q.group_by(PlanningRun.stage)):
        role = PLANNING_STAGE_ROLES.get(stage)
        if role:
            out[role] = out.get(role, 0) + int(n)
    # an open review counts only while its run still waits in REVIEW and no newer review superseded it (an
    # uncollected manual package of a cancelled/reopened run is history, not current activity)
    newer = select(ReviewRecord.supersedes_id).where(ReviewRecord.supersedes_id.is_not(None))
    q = select(ReviewRecord.reviewer_kind, func.count()).where(
        ReviewRecord.domain == domain, ReviewRecord.mode == DataMode.LIVE.value,
        ReviewRecord.run_id.in_(select(WorkerRun.id).where(WorkerRun.execution_status == S.REVIEW.value)),
        ReviewRecord.id.not_in(newer),
        or_(ReviewRecord.status.in_(["WAITING_QUOTA", "RUNNING", "PENDING"]),
            (ReviewRecord.status == "NEEDS_DECISION") & ReviewRecord.package_ref.is_not(None)))
    if project_ids is not None:
        q = q.where(ReviewRecord.project_id.in_(project_ids))
    for kind, n in session.execute(q.group_by(ReviewRecord.reviewer_kind)):
        role = REVIEWER_KIND_ROLES.get(kind)
        if role:
            out[role] = out.get(role, 0) + int(n)
    return out


def _assigned_task_counts(session: Session, *, domain: str, project_ids: list[str] | None) -> dict[str, int]:
    q = (select(TaskSpec.role_key, func.count()).join(TaskExecution, TaskExecution.task_id == TaskSpec.id)
         .where(TaskExecution.domain == domain, TaskExecution.state.not_in(["DONE", "FAILED", "CANCELLED"]))
         .group_by(TaskSpec.role_key))
    if project_ids is not None:
        q = q.where(TaskExecution.project_id.in_(project_ids))
    return {str(k): int(v) for k, v in session.execute(q).all()}


def roles_view(session: Session, *, domain: str, project_ids: list[str] | None = None) -> dict[str, Any]:
    live = _active_counts(session, WorkerRun.role_key, domain=domain, mode=DataMode.LIVE.value, project_ids=project_ids)
    for role, n in _supervision_counts(session, domain=domain, project_ids=project_ids).items():
        live[role] = live.get(role, 0) + n
    demo = _active_counts(session, WorkerRun.role_key, domain=domain, mode=DataMode.DEMO.value, project_ids=project_ids)
    assigned = _assigned_task_counts(session, domain=domain, project_ids=project_ids)
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g, _ in GROUPS}
    for r in session.scalars(select(RoleSkillPack).order_by(RoleSkillPack.name)):
        n = live.get(r.role_key, 0)
        groups.setdefault(r.group, []).append({
            "role_key": r.role_key, "name": r.name, "kind": r.kind, "group": r.group,
            "description": r.description, "outputs": r.outputs, "boundaries": r.boundaries,
            "verification": r.verification, "forbidden_actions": r.forbidden_actions,
            "deterministic_service": r.deterministic_service, "engine_hint": r.engine_hint,
            "source_section": r.source_section, "version": r.version, "revision_hash": r.revision_hash,
            "eval_status": r.eval_status, "mode": r.mode, "inputs": r.inputs or [],
            "example_outputs": r.example_outputs or [], "failure_modes": r.failure_modes or [],
            "allowed_scope": r.allowed_scope or {}, "evaluation_cases": r.evaluation_cases or [],
            "practice_sources": r.practice_sources or [],
            "active_runs": n, "demo_runs": demo.get(r.role_key, 0),
            # §17 assignment: non-terminal tasks of approved plans that name this role (projection, read-only)
            "assigned_tasks": assigned.get(r.role_key, 0),
            # an imported pack (§45) is a CANDIDATE: never runnable before review + evaluation, never "available"
            "availability": "ACTIVE" if n else ("CANDIDATE" if r.mode == "CANDIDATE" else
                                                "SERVICE" if r.deterministic_service else
                                                "HUMAN" if r.kind == "HUMAN" else "AVAILABLE_INACTIVE"),
        })
    return {"groups": [{"key": g, "label": GROUP_LABELS.get(g, g), "roles": groups.get(g, [])} for g, _ in GROUPS],
            "note": "Org view adalah proyeksi katalog: garis organisasi tidak memberi permission atau authority."}


def _agent_gate(session: Session, a: AgentDefinition, *, domain: str) -> tuple[bool, str]:
    """Availability of reviewer/verifier definitions follows the gate their owning module really applies
    (§17: the console must match what runs), not a flag nobody sets."""
    from aeos.core.enums import Capability
    from aeos.workforce import integrations as ext

    if a.harness == "claude-native":
        ok, reasons = ext.capability_ready(session, Capability.CAN_REVIEW_WITH_MAX, domain=domain, project_id=None)
        return ok, "CAN_REVIEW_WITH_MAX READY" if ok else ("; ".join(reasons)[:300] or "CAN_REVIEW_WITH_MAX not READY")
    if a.harness == "verifier-runner":
        ok, why = ext.verifier_available()
        return ok, "verification module present (trusted suite per project)" if ok else why
    return bool(a.enabled), a.enabled_reason


def _agent_in_scope(a: AgentDefinition, *, domain: str, project_ids: list[str] | None) -> bool:
    scope = a.allowed_scope or {}
    domains = scope.get("domains")
    if isinstance(domains, list) and domains and domain not in domains:
        return False
    projects = scope.get("project_ids")
    return not (isinstance(projects, list) and project_ids is not None and not set(projects) & set(project_ids))


def agents_view(session: Session, *, domain: str, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
    live = _active_counts(session, WorkerRun.agent_key, domain=domain, mode=DataMode.LIVE.value,
                          project_ids=project_ids)
    sup = _supervision_counts(session, domain=domain, project_ids=project_ids)
    out = []
    for a in session.scalars(select(AgentDefinition).order_by(AgentDefinition.agent_key)):
        if not _agent_in_scope(a, domain=domain, project_ids=project_ids):
            continue  # §35 project/domain-scoped definitions: an agent outside the scope is not listed
        enabled, why = _agent_gate(session, a, domain=domain)
        active = live.get(a.agent_key, 0) + (sup.get(a.role_key, 0) if a.harness == "claude-native" else 0)
        out.append({"agent_key": a.agent_key, "role_key": a.role_key, "harness": a.harness, "description": a.description,
                    "route_policy": a.route_policy, "tools": a.tools, "limits": a.limits, "allowed_scope": a.allowed_scope,
                    "enabled": enabled, "enabled_reason": why, "enabled_flag": a.enabled,
                    "enabled_source": "owning module gate" if a.harness in ("claude-native", "verifier-runner")
                    else "owner toggle (prerequisites checked)", "deterministic": a.deterministic,
                    "revision": a.revision, "revision_hash": a.revision_hash, "mode": a.mode, "owner": a.owner,
                    "active_runs": active,
                    "availability": "ACTIVE" if active else ("ENABLED_IDLE" if enabled else "DISABLED")})
    return out


def blockers(session: Session, *, domain: str, project_ids: list[str] | None) -> list[dict[str, Any]]:
    q = select(TaskExecution).where(TaskExecution.domain == domain, TaskExecution.mode == DataMode.LIVE.value,
                                    or_(TaskExecution.state == S.BLOCKED.value,
                                        (TaskExecution.state == S.READY.value) & (TaskExecution.reason_code != "")))
    if project_ids is not None:
        q = q.where(TaskExecution.project_id.in_(project_ids))
    out = []
    for te in session.scalars(q.limit(200)):
        out.append({"task_id": te.task_id, "task_key": te.task_key, "state": te.state, "reason_code": te.reason_code,
                    "reason": te.reason, "ui_state": derive_ui_state(None, te), "attempts_used": te.attempts_used,
                    "since": _iso(te.state_changed_at)})
    return out


def cost_summary(session: Session, *, domain: str, project_ids: list[str] | None) -> dict[str, Any]:
    q = select(func.count(), func.sum(UsageLedgerEntry.input_tokens), func.sum(UsageLedgerEntry.output_tokens),
               func.sum(UsageLedgerEntry.requests)).where(UsageLedgerEntry.domain == domain,
                                                          UsageLedgerEntry.mode == DataMode.LIVE.value,
                                                          UsageLedgerEntry.category == "WORKER_API")
    if project_ids is not None:
        q = q.where(UsageLedgerEntry.project_id.in_(project_ids))
    n, tin, tout, reqs = session.execute(q).one()
    known = select(func.sum(UsageLedgerEntry.cost_usd)).where(
        UsageLedgerEntry.domain == domain, UsageLedgerEntry.mode == DataMode.LIVE.value,
        UsageLedgerEntry.category == "WORKER_API", UsageLedgerEntry.cost_usd.is_not(None))
    unknown = select(func.count()).where(
        UsageLedgerEntry.domain == domain, UsageLedgerEntry.mode == DataMode.LIVE.value,
        UsageLedgerEntry.category == "WORKER_API", UsageLedgerEntry.cost_usd.is_(None))
    if project_ids is not None:
        known = known.where(UsageLedgerEntry.project_id.in_(project_ids))
        unknown = unknown.where(UsageLedgerEntry.project_id.in_(project_ids))
    known_usd = session.scalar(known)
    unknown_n = int(session.scalar(unknown) or 0)
    # Only quota of routes eligible for this domain (no OFFICE-only route metadata in PERSONAL).
    routes = [r for r in session.scalars(select(ModelRoute)) if domain in (r.eligible_domains or [])]
    route_keys = {r.route_key for r in routes}
    providers = {r.provider for r in routes}
    quota = [{"provider": q.provider, "scope": q.scope, "state": q.state, "source": q.source,
              "observed_at": _iso(q.observed_at), "resets_at": _iso(q.resets_at)}
             for q in session.scalars(select(QuotaState).order_by(QuotaState.updated_at.desc()).limit(200))
             if q.scope in route_keys or (q.scope in ("*", "") and q.provider in providers)][:50]
    return {"source": "usage_ledger (broker-recorded, LIVE only; DEMO/MOCK excluded)", "entries": int(n or 0),
            "input_tokens": int(tin or 0), "output_tokens": int(tout or 0), "requests": int(reqs or 0),
            "cost_known_usd": float(known_usd) if known_usd is not None else None,
            "cost_unknown_entries": unknown_n,
            "cost_status": "UNKNOWN" if unknown_n or (n and known_usd is None) else ("NONE" if not n else "KNOWN"),
            "quota": quota}


def summary(session: Session, *, domain: str, project_ids: list[str] | None, loop_running: bool) -> dict[str, Any]:
    from aeos.worker_runtime.facade import opencode_status, sandbox_readiness

    cap, cap_decision = scheduler.concurrency_cap(session)
    pause = scheduler.pause_info(session)
    live_q = select(func.count()).select_from(WorkerRun).where(
        WorkerRun.domain == domain, WorkerRun.mode == DataMode.LIVE.value,
        WorkerRun.execution_status.in_([S.LEASED.value, S.RUNNING.value]))
    demo_q = select(func.count()).select_from(WorkerRun).where(WorkerRun.domain == domain,
                                                                WorkerRun.mode != DataMode.LIVE.value)
    if project_ids is not None:  # counts follow the same project scope as the rows (no count leak)
        live_q = live_q.where(WorkerRun.project_id.in_(project_ids))
        demo_q = demo_q.where(WorkerRun.project_id.in_(project_ids))
    live_active = session.scalar(live_q) or 0
    demo_runs = session.scalar(demo_q) or 0
    sb = sandbox_readiness(session, domain=domain)
    return {
        "checked_at": utcnow().isoformat(), "domain": domain, "mode": DataMode.LIVE.value,
        "source": "TaskExecution/WorkerRun/Lease records (scheduler), usage ledger, sandbox probe",
        "scheduler": {"enabled": scheduler.scheduler_enabled(session), "paused": bool(pause.get("paused")),
                      "paused_reason": pause.get("reason", ""), "paused_by": pause.get("by"),
                      "concurrency_cap": cap, "cap_decision_id": cap_decision,
                      "controller_id": scheduler.CONTROLLER_ID, "loop_running": loop_running},
        "task_counts": scheduler.counts_by_state(session, domain=domain, project_ids=project_ids),
        "active_runs": int(live_active), "demo_runs": int(demo_runs),
        "blockers": blockers(session, domain=domain, project_ids=project_ids),
        "cost": cost_summary(session, domain=domain, project_ids=project_ids),
        "sandbox": {"status": sb["status"], "kind": sb.get("kind"), "reasons": sb.get("reasons", []),
                    "remedies": sb.get("remedies", []), "residual_risks": sb.get("residual_risks", []),
                    "levels": sb.get("capabilities")},
        "engine": opencode_status(),
    }
