"""`aeos seed` hook: load the dated research catalog (§45, §46, §47, §52). Idempotent; rows are
DESIGN-labelled candidates/references, never installation or usage evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import get_settings
from aeos.research.service import seed_registry


def run(session: Session) -> dict[str, Any]:
    return seed_registry(session, platform_repo_slug=get_settings().platform_repo_slug)
