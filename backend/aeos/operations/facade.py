"""Cross-module contract of ``aeos.operations`` (§09 released controller, §33 retirement). Keep stable."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.models.decisions import Decision
from aeos.models.portfolio import Project


def controller_report(settings: Settings, session: Session | None = None) -> dict[str, Any]:
    """Which code runs as the controller compared with the ACTIVE release, plus config drift (§09 zones)."""
    from aeos.operations.release import controller_report as impl

    return impl(settings, session)


def require_retirement_decision(session: Session, settings: Settings, project: Project) -> Decision:
    """Refuse lifecycle RETIRED unless the owner's retirement decision binds the current checklist (§33)."""
    from aeos.operations.retirement import require_retirement_decision as impl

    return impl(session, settings, project)
