"""Workforce & Scheduler records (§13, §14, §16, §17, §35).

RoleSkillPack = competence definition; AgentDefinition = configured executor (may be enabled but
idle); WorkerRun/Lease = actual activity. The organisation view is a projection and carries no
authority (§17).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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


class TaskExecution(IdMixin, TimestampMixin, Base):
    """Runtime execution state for one TaskSpec (separate from planning/publication/acceptance)."""

    __tablename__ = "task_executions"
    __table_args__ = (UniqueConstraint("task_id"),)

    task_id: Mapped[str] = mapped_column(ForeignKey("task_specs.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # optimistic concurrency


class WorkerRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "worker_runs"
    __table_args__ = (Index("ix_worker_runs_task", "task_id"), Index("ix_worker_runs_state", "execution_status"))

    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    run_type: Mapped[str] = mapped_column(String(32), default="IMPLEMENTATION")  # IMPLEMENTATION|VERIFICATION|REVIEW|TRIAL
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
