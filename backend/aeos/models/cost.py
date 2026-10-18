"""Cost & quota ledger (§42). Numbers come from code/provider data, never model guesses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import IdMixin, TimestampMixin


class UsageLedgerEntry(IdMixin, Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (Index("ix_usage_project_time", "project_id", "at"),)

    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # WORKER_API|NATIVE_CLAUDE|CI|COMPUTE|STORAGE
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    requests: Mapped[int] = mapped_column(default=1)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)  # None = UNKNOWN
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # provider|broker|estimator|ccusage
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class Budget(IdMixin, TimestampMixin, Base):
    __tablename__ = "budgets"

    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)  # GLOBAL|PROJECT|ROUTE|RUN
    scope_id: Mapped[str] = mapped_column(String(128), default="*")
    period: Mapped[str] = mapped_column(String(16), default="MONTH")  # DAY|WEEK|MONTH|TOTAL
    limit_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    limit_tokens: Mapped[int | None] = mapped_column(nullable=True)
    limit_requests: Mapped[int | None] = mapped_column(nullable=True)
    hard_stop: Mapped[bool] = mapped_column(Boolean, default=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # raising requires a decision
    notes: Mapped[str] = mapped_column(Text, default="")


class BudgetReservation(IdMixin, TimestampMixin, Base):
    __tablename__ = "budget_reservations"

    budget_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reserved_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    reserved_tokens: Mapped[int] = mapped_column(default=0)
    reserved_requests: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="RESERVED")  # RESERVED|COMMITTED|RELEASED


class QuotaState(IdMixin, TimestampMixin, Base):
    __tablename__ = "quota_states"

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(128), default="*")
    state: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # OK|LOW|EXHAUSTED|UNKNOWN
    source: Mapped[str] = mapped_column(String(64), default="UNKNOWN")  # official|provider-header|observed-429
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resets_at: Mapped[datetime | None] = mapped_column(nullable=True)
