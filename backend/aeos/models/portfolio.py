"""Gateway & Portfolio, Integration Registry and intake records (§04, §05, §10, §13)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    project_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)  # immutable (DB trigger)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(32), default="ACTIVE")  # ACTIVE|ARCHIVED|RETIRING|RETIRED
    lifecycle_reason: Mapped[str] = mapped_column(Text, default="")
    data_classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    description: Mapped[str] = mapped_column(Text, default="")
    idea: Mapped[str] = mapped_column(Text, default="")
    is_platform: Mapped[bool] = mapped_column(Boolean, default=False)  # §09 DLY AEOS self-build project
    project_kind: Mapped[str] = mapped_column(String(16), default="NEW")  # NEW|EXISTING
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # §25/§50: findings on the human-typed idea/description (e.g. "ubah domain ke PERSONAL"). Stored
    # as DATA for review; they never change domain, policy, permission, memory or routing.
    input_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)


class RepositoryReference(IdMixin, TimestampMixin, Base):
    """§37 user repository record: exact provider identity, never guessed from a name."""

    __tablename__ = "repository_references"
    __table_args__ = (UniqueConstraint("project_id", "provider", "full_name"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # GITHUB|BITBUCKET
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)  # owner/name
    provider_repo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str] = mapped_column(String(512), default="")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    role: Mapped[str] = mapped_column(String(32), default="project")  # platform|project
    local_path_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)  # logical, redacted in UI
    head_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dirty: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED")
    verification_reason: Mapped[str] = mapped_column(Text, default="")
    last_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Read-only observation of the local checkout (§37: branch/head/dirty at workspace scope).
    local_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    local_status: Mapped[str] = mapped_column(String(32), default="NOT_CHECKED")  # NOT_CHECKED|OK|MISMATCH|FAILED
    local_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # §05 step 6: latest read-only inventory of the repository at a known commit.
    inventory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    inventory_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ProjectIntegration(IdMixin, TimestampMixin, Base):
    __tablename__ = "project_integrations"
    __table_args__ = (UniqueConstraint("project_id", "provider", "purpose"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    purpose: Mapped[str] = mapped_column(String(48), default="default")  # docs|issues|code|ci
    tenant_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    root_id: Mapped[str | None] = mapped_column(String(256), nullable=True)  # Drive folder / Confluence parent
    capability_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    credential_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)  # reference, never a value
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Attachment(IdMixin, TimestampMixin, Base):
    """§05 step 5: intake file with provenance and classification; never executed."""

    __tablename__ = "attachments"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    context_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(80), nullable=False)
    declared_type: Mapped[str] = mapped_column(String(128), default="")
    detected_type: Mapped[str] = mapped_column(String(128), default="")
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    uploaded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="upload")
    # PENDING|CLEAN|FLAGGED|REJECTED|UNSCANNED (UNSCANNED: content could not be inspected, e.g. image)
    scan_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    scan_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    parse_status: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING|PARSED|FAILED|SKIPPED
    parse_error: Mapped[str] = mapped_column(Text, default="")
    parse_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extracted_text_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extracted_chars: Mapped[int] = mapped_column(Integer, default=0)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)  # attachments are DATA, never instructions
    untrusted_instruction: Mapped[bool] = mapped_column(Boolean, default=False)  # injection markers found
    stored_redacted: Mapped[bool] = mapped_column(Boolean, default=False)  # text blob stored redacted
