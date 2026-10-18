"""Publisher records: outbox, idempotency, external references, docs (§13, §27-§30)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class Publication(IdMixin, TimestampMixin, Base):
    __tablename__ = "publications"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    targets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # PREVIEW|PENDING|RUNNING|PUBLISHED|PARTIAL|FAILED|CONFLICT|CANCELLED
    status: Mapped[str] = mapped_column(String(16), default="PREVIEW")
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class PublicationOperation(IdMixin, TimestampMixin, Base):
    """Outbox row; side effects use idempotency keys and reconciliation, never exactly-once claims."""

    __tablename__ = "publication_operations"
    __table_args__ = (UniqueConstraint("idempotency_key"), Index("ix_pubop_publication", "publication_id"))

    publication_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)  # create_folder|create_doc|update_doc|create_issue
    logical_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    # PENDING|IN_FLIGHT|SUCCEEDED|FAILED|UNKNOWN|CONFLICT|SKIPPED
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    external_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ExternalReference(IdMixin, TimestampMixin, Base):
    __tablename__ = "external_references"
    __table_args__ = (UniqueConstraint("project_id", "provider", "logical_id"),)

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    logical_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    external_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    external_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sync_status: Mapped[str] = mapped_column(String(16), default="SYNCED")  # SYNCED|STALE|CONFLICT|UNKNOWN


class OperationalWorkProjection(IdMixin, TimestampMixin, Base):
    """§13: GitHub Issues/Jira status projection — never a second work authority."""

    __tablename__ = "operational_work_projections"
    __table_args__ = (UniqueConstraint("project_id", "provider", "issue_id"),)

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(128), nullable=False)
    issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    external_status: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sync_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN")


class DocumentRecord(IdMixin, TimestampMixin, Base):
    """§27 logical document identity for Product 1 nodes."""

    __tablename__ = "document_records"
    __table_args__ = (UniqueConstraint("project_id", "logical_doc_id"),)

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    logical_doc_id: Mapped[str] = mapped_column(String(128), nullable=False)  # e.g. "00-product-home"
    node: Mapped[str] = mapped_column(String(16), nullable=False)  # 00..12|TPL
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="DOC")  # DOC|FOLDER
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # DRAFT|APPROVED|PUBLISHED|SUPERSEDED|NOT_STARTED|NOT_APPLICABLE
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(16), default="AI_MAINTAINED")
    source_plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    external_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    links: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pending_sections: Mapped[list[str]] = mapped_column(JSON, default=list)  # "documentation pending"
