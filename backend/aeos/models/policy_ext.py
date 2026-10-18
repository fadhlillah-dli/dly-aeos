"""Policy & Decision Registry extension records (owned by ``aeos.policy``).

A DecisionRequest is a *request* for a human decision raised by a deterministic component
(scheduler, planner, publisher) or by a human. It carries no authority: it only feeds the single
Human Decision Queue (U-12). Resolving it always requires a separate human ``Decision`` row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class DecisionRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "decision_requests"
    __table_args__ = (Index("ix_decision_requests_status", "status", "domain"),)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # EXCEPTION|RISK_ACCEPTANCE|POLICY|BUDGET|...
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")  # why it waits (redacted DATA)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)  # component/user identity (DATA)
    requested_by_kind: Mapped[str] = mapped_column(String(16), default="SYSTEM")  # SYSTEM|HUMAN|AGENT
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    target_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requested_operations: Mapped[list[str]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN|RESOLVED|DECLINED|WITHDRAWN
    resolved_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
