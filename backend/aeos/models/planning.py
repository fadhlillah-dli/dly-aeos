"""Planning & Blueprint records (§13, §14, §21).

PlanVersion and TaskSpec are append-only (DB triggers): a reviewed version is never mutated —
human edits create a new version (§14).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class PlanningContext(IdMixin, TimestampMixin, Base):
    __tablename__ = "planning_contexts"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[list[Any]] = mapped_column(JSON, default=list)
    assumptions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    unknowns: Mapped[list[Any]] = mapped_column(JSON, default=list)
    facts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    questions: Mapped[list[Any]] = mapped_column(JSON, default=list)  # material questions only (§05)
    attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Attachment provenance + redacted text excerpts. Untrusted DATA (U-11, §25): never instructions.
    inputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    repo_inventory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # §05 step 6 existing project
    integration_capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")


class Plan(IdMixin, TimestampMixin, Base):
    """Planning lifecycle holder; state lives here, content lives in immutable PlanVersions."""

    __tablename__ = "plans"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    context_id: Mapped[str | None] = mapped_column(ForeignKey("planning_contexts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    state_reason: Mapped[str] = mapped_column(Text, default="")
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_baseline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # e.g. 4-stage Opus/Fable
    # Pipeline checkpoint (stage to resume, cycle artefact ids, manual package) — quota/bridge
    # interruptions resume here instead of skipping a stage (§12, §50 "Fable Max quota habis").
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlanningRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "planning_runs"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)  # PRIMARY_PLANNING|CHALLENGE_REVIEW|...
    role: Mapped[str] = mapped_column(String(64), nullable=False)  # Primary Planner|Planning Challenger|...
    model_requested: Mapped[str] = mapped_column(String(128), default="")
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engine: Mapped[str] = mapped_column(String(64), default="claude-code")
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="NATIVE")  # NATIVE|MANUAL_FALLBACK|MOCK
    config_identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING|RUNNING|SUCCEEDED|FAILED|...
    trace_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # reviewer != synthesizer
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    manual_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlanningArtifact(IdMixin, CreatedMixin, Base):
    __tablename__ = "planning_artifacts"

    planning_run_id: Mapped[str] = mapped_column(ForeignKey("planning_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # proposal|findings|synthesis|verdict
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("plan_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32), default="PLANNING_RUN")  # PLANNING_RUN|HUMAN_EDIT|IMPORT
    source_run_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class TaskSpec(Base):
    """§21 minimal TaskSpec contract. Immutable; changes produce a new PlanVersion."""

    __tablename__ = "task_specs"
    __table_args__ = (UniqueConstraint("plan_version_id", "task_key"), Index("ix_task_specs_project", "project_id"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), nullable=False)
    plan_version_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    role_key: Mapped[str] = mapped_column(String(64), default="feature-implementer")
    # Revision of the executor RoleSkillPack the spec was compiled against (part of spec_hash, §19 diagram
    # "katalog role/skill -> task compiler"); None = role_key is not a catalog executor (never admitted).
    role_revision_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_artifacts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expected_outputs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)  # task_keys within the version
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    forbidden_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    tools_allowed: Mapped[list[str]] = mapped_column(JSON, default=list)
    egress_allowed: Mapped[list[str]] = mapped_column(JSON, default=list)
    capability_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    capability_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    data_classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    model_route_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reviewer_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verifier_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # time/resource/token/attempts
    stop_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_requirements: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risk_class: Mapped[str] = mapped_column(String(4), default="R1")
    artifact_schema_version: Mapped[str] = mapped_column(String(16), default="1")
    # Provenance copied from the PlanVersion (LIVE|MOCK|DEMO): a TaskSpec compiled from fixture plan
    # content is itself a fixture and must never be admitted as real work (§35, §49).
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    spec_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class TaskDependency(IdMixin, Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("plan_version_id", "task_id", "depends_on_task_id"),)

    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_specs.id"), nullable=False)
    depends_on_task_id: Mapped[str] = mapped_column(ForeignKey("task_specs.id"), nullable=False)


class Feature(IdMixin, CreatedMixin, Base):
    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("plan_version_id", "feature_key"),)

    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    requirement: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="P2")
    acceptance: Mapped[list[Any]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    milestone_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Milestone(IdMixin, CreatedMixin, Base):
    __tablename__ = "milestones"
    __table_args__ = (UniqueConstraint("plan_version_id", "milestone_key"),)

    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), nullable=False)
    milestone_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    exit_evidence: Mapped[str] = mapped_column(Text, default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # §13 "requirement, priority, acceptance, dependency, owner" (from plan milestone_governance)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    acceptance: Mapped[list[Any]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
