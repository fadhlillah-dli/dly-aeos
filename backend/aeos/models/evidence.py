"""Verification & Review records (§13, §34).

Worker attachments are untrusted input evidence. Only the independent collector/verifier writes
trusted EvidenceArtifacts (trusted=True) with content hash, timestamp and collector identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class EvidenceArtifact(Base):
    __tablename__ = "evidence_artifacts"
    __table_args__ = (Index("ix_evidence_run", "run_id"), Index("ix_evidence_subject", "subject_type", "subject_id"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(64), default="")  # run|task|check|component|proposal
    subject_id: Mapped[str] = mapped_column(String(128), default="")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(48), nullable=False)  # TEST_REPORT|LOG|DIFF|SCREENSHOT|PROBE|...
    origin: Mapped[str] = mapped_column(String(64), nullable=False)  # verifier|doctor|collector|worker|human
    collector_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)  # relative to data dir
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # small redacted summary
    evidence_level: Mapped[str] = mapped_column(String(4), default="E0")
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # commit/config/env identity
    retention_policy: Mapped[str] = mapped_column(String(64), default="default")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class ReviewRecord(IdMixin, CreatedMixin, Base):
    """§34 REVIEW / VERIFY / VERDICT."""

    __tablename__ = "review_records"

    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewer_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # CLAUDE|HUMAN|SECURITY|MOCK
    reviewer_identity: Mapped[str] = mapped_column(String(128), default="")
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verify: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    unknowns: Mapped[list[Any]] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class AcceptanceRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "acceptance_records"

    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plan_version_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verifier_evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    review_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acceptance_status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|ACCEPTED|REJECTED|STALE|INVALIDATED
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
