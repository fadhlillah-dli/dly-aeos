"""Cross-module contract for projects (§04, §13). Owned by ``aeos.portfolio``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.models.portfolio import Project, RepositoryReference

PLATFORM_PROJECT_KEY = "dly-aeos"


def get_project(session: Session, project_id: str) -> Project | None:
    return session.get(Project, project_id)


def platform_project(session: Session) -> Project | None:
    """The DLY AEOS self-build project (§09), registered as PERSONAL by `aeos seed`."""
    return session.scalar(select(Project).where(Project.is_platform.is_(True)))


def repositories(session: Session, project_id: str) -> list[RepositoryReference]:
    return list(session.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project_id)))
