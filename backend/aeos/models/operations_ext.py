"""Operations & Reliability records (§33, §35, §41). Owned by ``aeos.operations``.

* ``OpsAlert`` — deduplicated alert state; every new/re-opened alert is also emitted as an
  ``alert.raised`` event (the event stream stays the dashboard source, this table only dedups).
* ``OpsExportSnapshot`` — registry of exported snapshots (id, hash, scope, time). An export is a
  snapshot, never a live view (§41); the registry lets a human verify an export file later.

Backups and releases are recorded on disk next to their artifacts (manifest/provenance files) so a
database restore can never make the recovery points themselves disappear.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import CreatedMixin, IdMixin


class OpsAlert(IdMixin, Base):
    __tablename__ = "ops_alerts"
    __table_args__ = (Index("ix_ops_alerts_status", "status"), Index("ix_ops_alerts_domain", "domain"))

    dedup_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # stale_leases|sli_breach|restore_untested|...
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # redacted
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None = platform-level
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="sli")  # sli|release|backup|emergency
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN|RESOLVED
    auto_resolve: Mapped[bool] = mapped_column(Boolean, default=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")


class OpsExportSnapshot(IdMixin, CreatedMixin, Base):
    __tablename__ = "ops_export_snapshots"
    __table_args__ = (Index("ix_ops_export_kind", "kind"),)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # inventory|architecture|evidence|audit|decisions
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    redactions_applied: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
