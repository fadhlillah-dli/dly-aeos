"""Model catalog refresh history (§23, §41 freshness). Owned by ``aeos.routing``.

One row per refresh attempt with per-source results (OpenRouter catalog, OpenRouter key info,
OpenCode catalog). Rows record what was observed and when; they never activate a route.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import CreatedMixin, IdMixin


class CatalogRefresh(IdMixin, CreatedMixin, Base):
    __tablename__ = "route_catalog_refreshes"

    actor: Mapped[str] = mapped_column(String(64), default="system")
    status: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # OK|PARTIAL|FAILED
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # source -> summary (redacted)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
