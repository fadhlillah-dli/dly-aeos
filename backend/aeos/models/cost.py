"""Cost & quota ledger (§42). Numbers come from code/provider data, never model guesses.

Owned by ``aeos.cost``. Monetary columns are ``Numeric`` (Decimal on read); the service does all
budget arithmetic in ``Decimal`` so ``committed + reserved + request <= limit`` is exact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import IdMixin, TimestampMixin

# §42: units are monitored separately — never summed into one number.
USAGE_CATEGORIES = ("WORKER_API", "NATIVE_CLAUDE", "CI", "COMPUTE", "STORAGE", "HUMAN_TIME")


class UsageLedgerEntry(IdMixin, Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        Index("ix_usage_project_time", "project_id", "at"),
        Index("ix_usage_reservation", "reservation_id"),
        Index("ix_usage_run", "run_id"),
    )

    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # see USAGE_CATEGORIES
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    requests: Mapped[int] = mapped_column(default=1)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)  # None = UNKNOWN
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    # Conservative amount charged against budgets when the actual cost is UNKNOWN (the reserved
    # estimate). Never shown as an actual cost.
    charged_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Non-token units (CI minutes, compute seconds, storage bytes, human minutes).
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # provider|broker|estimator|ccusage
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class Budget(IdMixin, TimestampMixin, Base):
    """A spending/usage cap. Limits are never edited in place: a change creates a new row bound to
    an owner BUDGET decision and the previous row becomes SUPERSEDED (§42, AGENTS.md invariant 3)."""

    __tablename__ = "budgets"
    __table_args__ = (Index("ix_budgets_scope", "scope_type", "scope_id", "period", "status"),)

    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)  # GLOBAL|PROJECT|ROUTE|RUN
    scope_id: Mapped[str] = mapped_column(String(128), default="*")
    period: Mapped[str] = mapped_column(String(16), default="MONTH")  # DAY|WEEK|MONTH|TOTAL
    limit_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    limit_tokens: Mapped[int | None] = mapped_column(nullable=True)
    limit_requests: Mapped[int | None] = mapped_column(nullable=True)
    hard_stop: Mapped[bool] = mapped_column(Boolean, default=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # raising requires a decision
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE|SUPERSEDED
    terms_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)  # == decision.target_hash
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)  # PROJECT budgets: project domain
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # the $0 hard-stop GLOBAL default


class BudgetReservation(IdMixin, TimestampMixin, Base):
    """One admission hold for one request; it counts against every budget whose scope matches its
    project/route/run (``budget_ids`` lists those checked at admission time)."""

    __tablename__ = "budget_reservations"
    __table_args__ = (Index("ix_reservations_status", "status"),)

    budget_id: Mapped[str] = mapped_column(String(36), nullable=False)  # the GLOBAL budget checked
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budget_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reserved_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    reserved_tokens: Mapped[int] = mapped_column(default=0)
    reserved_requests: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="RESERVED")  # RESERVED|COMMITTED|RELEASED
    committed_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    committed_tokens: Mapped[int | None] = mapped_column(nullable=True)
    committed_requests: Mapped[int | None] = mapped_column(nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    pricing_basis: Mapped[str] = mapped_column(String(32), default="UNKNOWN")  # FREE_ROUTE|ROUTE_PRICING|CALLER_ESTIMATE


class QuotaState(IdMixin, TimestampMixin, Base):
    __tablename__ = "quota_states"
    __table_args__ = (UniqueConstraint("provider", "scope"),)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(128), default="*")
    state: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # OK|LOW|EXHAUSTED|UNKNOWN
    source: Mapped[str] = mapped_column(String(64), default="UNKNOWN")  # official|provider-header|observed-429
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resets_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CostAdmissionLock(Base):
    """Single-row write lock: every reservation first bumps this row so the SQLite write lock is
    held *before* budget state is read. Concurrent admissions therefore serialise and re-check
    inside their own transaction (§14 "Budget/reservation diperbarui atomik")."""

    __tablename__ = "cost_admission_lock"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
