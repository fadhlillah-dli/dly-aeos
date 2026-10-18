"""Knowledge & Context extension records (§24). Owned by ``aeos.knowledge``.

``ContextPack`` and ``MemoryRecord`` (canonical §13 records) live in ``aeos.models.research``; this
file only adds the build metadata a pack needs for audit/staleness without duplicating the concept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import TimestampMixin


class ContextPackMeta(TimestampMixin, Base):
    """How a ContextPack was built: request, repository/commit, budget use, omissions, staleness."""

    __tablename__ = "context_pack_meta"

    context_pack_id: Mapped[str] = mapped_column(
        ForeignKey("context_packs.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    repository_full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # query/paths/include as requested
    include_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    forbidden_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    omitted: Mapped[list[Any]] = mapped_column(JSON, default=list)  # {kind, ref, reason, tokens}
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    observed_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # §13 ContextPack "TTL/expiry, supersession": a pack is usable only until expires_at, and a newer
    # pack for the same project/task/role/repository supersedes (and stales) the older one.
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
