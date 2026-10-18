"""ClaudeNativeBridge records (§12, §08). Owned by ``aeos.claude_bridge``.

Capability tests are kept as history so the dashboard can show the last proof time, the binary
fingerprint it was proven against and whether a later binary/version change made it STALE.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import CreatedMixin, IdMixin


class ClaudeCapabilityTest(IdMixin, CreatedMixin, Base):
    __tablename__ = "claude_capability_tests"

    status: Mapped[str] = mapped_column(String(16), nullable=False)  # VERIFIED|DEGRADED|FAILED|BLOCKED
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    model_requested: Mapped[str] = mapped_column(String(64), default="")
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binary_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    auth_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # no tokens, ever
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
