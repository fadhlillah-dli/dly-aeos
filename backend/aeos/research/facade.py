"""Cross-module contract for the Research & Technology Registry (§45, §47). Owned by ``aeos.research``.

Registry rows are candidates/references; they never grant installation, activation or trust.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.models.research import ResearchEntry


def canonical_identity(ref: str) -> str:
    """Normalise a URL/name into ``github:owner/repo`` / ``url:host/path`` / ``name:slug``."""
    from aeos.research.identity import canonical_identity as impl

    return impl(ref)


def entries_for_component(session: Session, component_id: str) -> list[ResearchEntry]:
    return list(session.scalars(select(ResearchEntry).where(ResearchEntry.component_id == component_id)))
