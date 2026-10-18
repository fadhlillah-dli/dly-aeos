"""Publisher records: outbox, idempotency, external references, docs (§13, §27-§30).

Owned by ``aeos.publisher`` (publication/outbox/external refs/work projection) and
``aeos.docs_engine`` (document records/revisions).

* ``Publication`` is one publication run of exactly one APPROVED PlanVersion (bound by id + hash).
* ``PublicationOperation`` is the outbox: one row per external side effect, keyed by an
  idempotency key ``hash(project, provider, logical_id, operation, approved_hash)``. There is no
  exactly-once claim across APIs (§14); unknown outcomes are reconciled before any retry.
* ``ExternalReference`` maps a logical id (doc/folder/issue/label) to the provider's id/revision.
* ``OperationalWorkProjection`` mirrors GitHub Issues/Jira status — a projection, never authority.
* ``DocumentRecord`` is the logical Product 1 document identity (§27 metadata);
  ``DocumentRevision`` keeps rendered snapshots and human edits observed at the provider
  (revision input, §29/§41) so a stale snapshot never silently overwrites newer human content.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class Publication(IdMixin, TimestampMixin, Base):
    __tablename__ = "publications"
    __table_args__ = (Index("ix_publications_project", "project_id"),)

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    # The plan APPROVAL decision bound to plan_version_id + approved_hash (looked up at preview).
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    targets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # PREVIEW|PENDING|RUNNING|PUBLISHED|PARTIAL|FAILED|CONFLICT|CANCELLED
    status: Mapped[str] = mapped_column(String(16), default="PREVIEW")
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # --- publisher extensions
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # The PUBLICATION decision (human "Approve & Publish") covering the concrete ops of this run (U-10).
    publication_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PublicationOperation(IdMixin, TimestampMixin, Base):
    """Outbox row; side effects use idempotency keys and reconciliation, never exactly-once claims."""

    __tablename__ = "publication_operations"
    __table_args__ = (UniqueConstraint("idempotency_key"), Index("ix_pubop_publication", "publication_id"))

    publication_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    # create_folder|create_doc|update_doc|ensure_label|create_issue|upload_attachment
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
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
    # --- publisher extensions
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="")


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
    # --- publisher extensions
    kind: Mapped[str] = mapped_column(String(24), default="")  # folder|doc|page|issue|label|attachment
    title: Mapped[str] = mapped_column(String(256), default="")
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)  # last published content
    last_publication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


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
    # --- publisher extensions
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    logical_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feature_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    sync_error: Mapped[str] = mapped_column(Text, default="")


class DocumentRecord(IdMixin, TimestampMixin, Base):
    """§27 logical document identity for Product 1 nodes."""

    __tablename__ = "document_records"
    __table_args__ = (UniqueConstraint("project_id", "logical_doc_id"),)

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    logical_doc_id: Mapped[str] = mapped_column(String(128), nullable=False)  # e.g. "00-product-home"
    node: Mapped[str] = mapped_column(String(16), nullable=False)  # 00..12|TPL|ROOT
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
    # --- docs engine extensions
    parent_logical_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(48), default="")  # PRODUCT_HOME|PRD|TEST_PLAN|FOLDER|...
    template_key: Mapped[str | None] = mapped_column(String(48), nullable=True)
    timing: Mapped[str] = mapped_column(String(32), default="")  # §28 PROJECT_CREATION|FEATURE_APPROVAL|...
    input_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    rendered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # §27 "last reviewed": a human review of exactly this content hash (not an approval; audited).
    last_reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)


class DocumentRevision(IdMixin, CreatedMixin, Base):
    """Rendered snapshot or observed human edit for one logical document (§29, §30, §41).

    ``kind=RENDERED``: deterministic snapshot rendered from an approved PlanVersion.
    ``kind=HUMAN_EDIT_INPUT``: the provider revision changed outside AEOS (requiredRevisionId /
    version mismatch). It is revision INPUT for the next plan version — never overwritten.
    """

    __tablename__ = "document_revisions"
    __table_args__ = (Index("ix_docrev_doc", "document_id"),)

    document_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    logical_doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # RENDERED|HUMAN_EDIT_INPUT
    # RENDERED: CURRENT|SUPERSEDED ; HUMAN_EDIT_INPUT: OPEN|ACKNOWLEDGED
    status: Mapped[str] = mapped_column(String(16), default="CURRENT")
    plan_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str | None] = mapped_column(String(48), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expected_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    publication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
