"""Delivery & CI records (§31). Owned by ``aeos.delivery``.

A PullRequestRecord tracks the PR the trusted broker opened for an accepted task. It observes the
provider (head/base SHA, merged_by) — it never merges. Merge to main stays a human action (U-09);
``merged_by`` is what GitHub reports, recorded as an observation, not as authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class PullRequestRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "delivery_pull_requests"
    __table_args__ = (
        Index("ix_delivery_pr_task", "task_id"),
        Index("ix_delivery_pr_repo", "repo_full_name", "number"),
    )

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    acceptance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="GITHUB")
    repo_full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    head_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    accepted_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initial_base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bundle_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    bundle_evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # PREPARED|OPEN|CLOSED|MERGED|UNKNOWN — observed provider state; AEOS itself never merges.
    state: Mapped[str] = mapped_column(String(16), default="PREPARED")
    merged: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_by_login: Mapped[str | None] = mapped_column(String(128), nullable=True)
    merged_by_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # User|Bot|Organization
    merged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sync_status: Mapped[str] = mapped_column(String(16), default="NEVER")  # NEVER|SYNCED|FAILED|UNKNOWN
    sync_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
