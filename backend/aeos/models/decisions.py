"""Policy & Decision Registry (§10, §13, §20, §40).

A Decision binds an actor with AUTH to an exact target/version/hash and operation scope. Binding
columns are immutable (DB trigger); only lifecycle columns (status/invalidation) may change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class Decision(IdMixin, CreatedMixin, Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_target", "target_type", "target_id"),)

    # APPROVAL|REJECTION|REVISION_REQUEST|EXCEPTION|ADR|POLICY|TRIAL|PROMOTION|RISK_ACCEPTANCE|
    # PUBLICATION|DOMAIN_SELECTION|ROUTE_ACTIVATION|BUDGET
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), default="HUMAN")  # decisions are human-only AUTH
    auth_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    allowed_operations: Mapped[list[str]] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE|EXPIRED|INVALIDATED|SUPERSEDED
    invalidated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class PolicySnapshot(IdMixin, CreatedMixin, Base):
    __tablename__ = "policy_snapshots"

    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="owner-decisions")
    mci_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # status UNVERIFIED until readable
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class ArchitectureDecisionRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "adrs"

    adr_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # ADR-0001
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED")  # PROPOSED|ACCEPTED|SUPERSEDED|REJECTED
    context: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(Text, default="")
    consequences: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ArchitectureSnapshot(IdMixin, CreatedMixin, Base):
    """§13 immutable references/hashes of approved decisions/manifest/contracts for render/diff."""

    __tablename__ = "architecture_snapshots"

    label: Mapped[str] = mapped_column(String(128), default="")
    refs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
