"""Research & Technology Registry, ChangeProposal, evaluation (§39, §40, §43, §44, §47)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class ResearchEntry(IdMixin, TimestampMixin, Base):
    __tablename__ = "research_entries"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    release_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    research_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    maintainer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claims: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_level: Mapped[str] = mapped_column(String(4), default="E0")
    relevance: Mapped[str] = mapped_column(Text, default="")
    placement: Mapped[str] = mapped_column(Text, default="")  # §45 placement decision
    data_terms: Mapped[str] = mapped_column(Text, default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    compatibility: Mapped[str] = mapped_column(Text, default="")
    test_results: Mapped[list[Any]] = mapped_column(JSON, default=list)
    owner_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # DISCOVERED|SOURCE_VERIFIED|CONTROLLED_EVAL|CANARY|RECOMMENDED_FOR_PROMOTION|REJECTED|DEFERRED|ENABLED|REFERENCE_ONLY
    lifecycle: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    component_id: Mapped[str | None] = mapped_column(String(96), nullable=True)


class ChangeProposal(IdMixin, TimestampMixin, Base):
    __tablename__ = "change_proposals"

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # ADD|REPLACE|UPGRADE|REMOVE|COMPARE
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), default="PLATFORM")  # PLATFORM|PROJECT
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # URLs or names
    problem: Mapped[str] = mapped_column(Text, default="")
    expected_benefit: Mapped[str] = mapped_column(Text, default="")
    existing_component_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_component_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # cost/data/resource
    baseline_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    candidate_pin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # SUBMITTED|UNDER_REVIEW|REVIEWED|TRIAL_APPROVED|IN_TRIAL|PROMOTION_PENDING|ACCEPTED|ROLLED_BACK|FAILED|PARTIAL|
    # KEEP|REJECTED|DEFERRED|REQUEST_MORE_EVIDENCE|REFERENCE_ONLY
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTED")
    review_package: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # §39 items 1-10
    decision_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class EvalSuite(IdMixin, TimestampMixin, Base):
    __tablename__ = "eval_suites"

    suite_key: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_class: Mapped[str] = mapped_column(String(64), default="")  # bugfix|feature-fe-api|docs|...
    version: Mapped[str] = mapped_column(String(32), default="1")
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    held_out: Mapped[bool] = mapped_column(default=True)
    cases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    protected: Mapped[bool] = mapped_column(default=True)  # workers cannot modify graders


class EvalRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"

    suite_key: Mapped[str] = mapped_column(String(96), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # full AIConfiguration
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    topology: Mapped[str | None] = mapped_column(String(32), nullable=True)  # A|B|C|D (§44)
    status: Mapped[str] = mapped_column(String(32), default="PLANNED")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # §43 dimensions
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list)  # never dropped
    gate_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ContextPack(IdMixin, TimestampMixin, Base):
    """§24 curated task context with provenance and budget."""

    __tablename__ = "context_packs"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items: Mapped[list[Any]] = mapped_column(JSON, default=list)  # {kind, ref, provenance, classification}
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    pack_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stale: Mapped[bool] = mapped_column(default=False)


class MemoryRecord(IdMixin, TimestampMixin, Base):
    """§24 accepted fact/decision with scope, AUTH, expiry and supersession; never secrets."""

    __tablename__ = "memory_records"

    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), default="PROJECT")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    auth_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")  # PROPOSED|ACCEPTED|EXPIRED|SUPERSEDED|REJECTED
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
