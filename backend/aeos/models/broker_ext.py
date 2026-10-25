"""Capability-broker operation records (§25, §26, §31). Owned by ``aeos.broker``.

``BrokerRequest`` is one model-proxy call observed by the broker (the trusted source for worker
model usage, not the worker's own narration). ``BrokerSideEffect`` is the outbox/reconciliation
record for external side effects (task-branch push, PR creation): cancel does NOT undo these, so
they are kept visible on Run Detail (§35 "tidak diasumsikan membatalkan side effect eksternal").
Neither table ever stores a credential or capability token.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class BrokerRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "broker_requests"
    __table_args__ = (Index("ix_broker_requests_run", "run_id"), Index("ix_broker_requests_grant", "grant_id"))

    grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exposed_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    upstream_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    model_resolved: Mapped[str | None] = mapped_column(String(160), nullable=True)  # as reported by upstream
    stream: Mapped[bool] = mapped_column(default=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # OK|RATE_LIMITED|UPSTREAM_ERROR|DENIED|CAP_EXCEEDED|UNKNOWN_MODEL|TIMEOUT
    outcome: Mapped[str] = mapped_column(String(24), default="PENDING")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)  # None = UNKNOWN
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str] = mapped_column(Text, default="")  # always redacted
    # §23 "mode/thinking params" (ADR-0041 amendment 2026-10-25): the reasoning level the broker sent upstream
    # (the route's mode_params.reasoning; NOT_SENT = provider default) and the level the worker asked for, which
    # is never forwarded. None on rows recorded before the column existed (UNKNOWN).
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasoning_requested: Mapped[str | None] = mapped_column(String(32), nullable=True)


class BrokerSideEffect(IdMixin, TimestampMixin, Base):
    __tablename__ = "broker_side_effects"
    __table_args__ = (UniqueConstraint("idempotency_key"), Index("ix_broker_side_effects_run", "run_id"))

    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # PUSH_BRANCH|CREATE_PR
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # repo/branch/base (no credentials)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|SUCCEEDED|FAILED|UNKNOWN
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_summary: Mapped[str] = mapped_column(Text, default="")  # redacted
