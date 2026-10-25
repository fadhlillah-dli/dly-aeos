"""Workforce & Scheduler records (§13, §14, §16, §17, §35).

RoleSkillPack = competence definition; AgentDefinition = configured executor (may be enabled but
idle); WorkerRun/Lease = actual activity. The organisation view is a projection and carries no
authority (§17).

Separate status fields are kept deliberately (§13, §35): TaskExecution.state is the canonical
runtime state of a task; WorkerRun.execution_status is the state of one attempt; worker-reported,
verifier, review, acceptance and merged status are independent observed fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class RoleSkillPack(IdMixin, TimestampMixin, Base):
    __tablename__ = "role_skill_packs"

    role_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    group: Mapped[str] = mapped_column(String(64), nullable=False)  # §17 org view groups
    kind: Mapped[str] = mapped_column(String(32), default="EXECUTOR")  # PLANNER|REVIEWER|EXECUTOR|SERVICE|HUMAN
    description: Mapped[str] = mapped_column(Text, default="")
    outputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # §17 competence definition: what the role consumes, where it may act, example outputs (§46)
    inputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    allowed_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    example_outputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    boundaries: Mapped[list[Any]] = mapped_column(JSON, default=list)
    forbidden_actions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verification: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evaluation_cases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    failure_modes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    practice_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    deterministic_service: Mapped[bool] = mapped_column(Boolean, default=False)  # §16 scheduler, broker, ...
    version: Mapped[str] = mapped_column(String(32), default="1")
    revision_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    eval_status: Mapped[str] = mapped_column(String(32), default="NOT_EVALUATED")
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")
    # Model/engine guidance from §15 (e.g. "Opus family") — descriptive, never a route activation.
    engine_hint: Mapped[str] = mapped_column(String(160), default="")
    source_section: Mapped[str] = mapped_column(String(16), default="")  # §15 / §16


class AgentDefinition(IdMixin, TimestampMixin, Base):
    __tablename__ = "agent_definitions"

    agent_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role_key: Mapped[str] = mapped_column(String(64), nullable=False)
    harness: Mapped[str] = mapped_column(String(64), nullable=False)  # opencode|claude-native|verifier-runner
    route_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    owner: Mapped[str] = mapped_column(String(128), default="owner")
    allowed_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[str] = mapped_column(String(32), default="1")
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")
    description: Mapped[str] = mapped_column(Text, default="")
    deterministic: Mapped[bool] = mapped_column(Boolean, default=False)
    revision_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled_reason: Mapped[str] = mapped_column(Text, default="")


class TaskExecution(IdMixin, TimestampMixin, Base):
    """Runtime execution state for one TaskSpec (separate from planning/publication/acceptance)."""

    __tablename__ = "task_executions"
    __table_args__ = (UniqueConstraint("task_id"), Index("ix_task_exec_state", "state"))

    task_id: Mapped[str] = mapped_column(ForeignKey("task_specs.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # optimistic concurrency
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Machine-readable wait/block reason (e.g. SHARED_WRITE_CONFLICT, NEEDS_DECISION, LEASE_LOST).
    reason_code: Mapped[str] = mapped_column(String(48), default="")
    state_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    assigned_agent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)  # reviewer findings for repair
    review_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    extra_attempts: Mapped[int] = mapped_column(Integer, default=0)  # only via an owner EXCEPTION decision
    extra_attempts_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # human who executed the plan
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # §21 "base commit": pinned once when the plan is executed; every attempt starts from it. A base change
    # is an explicit owner action (re-pin), never a silent move to the newest HEAD.
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WorkerRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "worker_runs"
    __table_args__ = (Index("ix_worker_runs_task", "task_id"), Index("ix_worker_runs_state", "execution_status"))

    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    run_type: Mapped[str] = mapped_column(String(32), default="IMPLEMENTATION")  # IMPLEMENTATION|INVESTIGATION|VERIFICATION|REVIEW|TRIAL
    agent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)
    block_reason: Mapped[str] = mapped_column(Text, default="")
    model_route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sandbox_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # process|container|vm
    sandbox_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    workspace_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_action: Mapped[str] = mapped_column(Text, default="")  # observed tool action, not narration
    # Who wrote current_action (§35 Live Work): RUNNER (observed tool action via record_action) or CONTROLLER
    # (status text from executor/broker/scheduler). Set by the attribute listener below, never by the UI.
    current_action_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    touched_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # §35: these are separate fields — never merged into one check mark.
    worker_reported_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verifier_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acceptance_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merged_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caps: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    budget_reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capability_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # --- module E extensions (§14 reconciliation, §21 scope, §35 Run Detail) ---
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pid_create_time: Mapped[float | None] = mapped_column(Float, nullable=True)  # psutil identity of the pid
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    quarantine_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    diff_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    review_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    acceptance_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    usage_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quota_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_code: Mapped[str] = mapped_column(String(48), default="")
    # Trusted git directory of the candidate (outside the worker-editable tree; ADR-0054). Runtime
    # and broker git operations use it explicitly instead of the worktree's own ``.git`` pointer.
    git_dir_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Revision of the role/agent definition this attempt ran with (§50 "Role/model/mode diganti":
    # configuration is versioned; history is never rewritten).
    role_revision_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    agent_revision_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)


class Lease(IdMixin, TimestampMixin, Base):
    __tablename__ = "leases"
    __table_args__ = (Index("ix_leases_task_state", "task_id", "state"),)

    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)  # controller instance id
    acquired_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE|RELEASED|EXPIRED|REVOKED
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=120)
    release_reason: Mapped[str] = mapped_column(Text, default="")


# ----------------------------------------------------------------------------- current-action provenance (§35)
CURRENT_ACTION_RUNNER = "RUNNER"  # observed tool action reported through scheduler.record_action
CURRENT_ACTION_CONTROLLER = "CONTROLLER"  # status text written by executor/broker/scheduler


@event.listens_for(WorkerRun.current_action, "set", propagate=True)
def _current_action_source(target: WorkerRun, value: Any, oldvalue: Any, initiator: Any) -> None:
    """The source label follows the writer: only ``record_action`` marks its write as a runner action
    (``_runner_action_write``); every other assignment is controller status text."""
    runner = bool(getattr(target, "_runner_action_write", False))
    target.current_action_source = CURRENT_ACTION_RUNNER if runner else CURRENT_ACTION_CONTROLLER
