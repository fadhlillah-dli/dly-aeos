"""Readiness Doctor run grouping (§08). Owned by ``aeos.doctor``.

A DoctorRun groups the CheckResult rows produced by one invocation (CLI or dashboard) so the UI
can show which checks ran together, with which options (group/capability/functional), and when.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import IdMixin


class DoctorRun(IdMixin, Base):
    __tablename__ = "doctor_runs"
    __table_args__ = (Index("ix_doctor_runs_started", "started_at"),)

    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="cli")  # cli|api
    actor: Mapped[str] = mapped_column(String(64), default="local-operator")
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = all groups
    capability: Mapped[str | None] = mapped_column(String(48), nullable=True)
    functional: Mapped[bool] = mapped_column(Boolean, default=False)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # status -> n
    host: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # platform/arch only, no PII
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
