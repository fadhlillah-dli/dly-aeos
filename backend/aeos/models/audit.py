"""AuditEvent and persistent Event stream (§13, §35).

Events are persisted first and then projected for the dashboard; sequence numbers give clients a
cursor for reconnect/deduplication.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow


class AuditEvent(Base):
    __tablename__ = "audit_events"
    # (action, at): the OpenRouter free-model count reads the probes of the last minute / UTC day (decision C, 0010)
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id"), Index("ix_audit_project", "project_id"),
                      Index("ix_audit_action_at", "action", "at"))

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)  # user|system|worker|service|cli
    actor_id: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # SUCCESS|DENIED|FAILED
    reason: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # always redacted
    request_id: Mapped[str] = mapped_column(String(64), default="")
    # §13 AuditEvent "actor/run/target": the worker/verification run an action belongs to, when any.
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_entity", "entity_type", "entity_id"), Index("ix_events_project", "project_id"))

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # controller|doctor|broker|runner|...
    kind: Mapped[str] = mapped_column(String(96), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(128), default="")
    entity_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")  # LIVE|MOCK|DEMO
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
