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


# ---- additive (non-breaking) contract ---------------------------------------------------------------


def inventory_for_project(session: Session, project_id: str, *, commit: str | None = None) -> dict:
    """Read-only inventory of the project's registered primary repository (ADR-0022 §6) for
    ``PlanningContext.repo_inventory``. Raises ``NotFound``/``Blocked`` (no repository/checkout) and
    ``PolicyDenied`` when the checkout crosses the domain boundary. Never guesses a path."""
    from aeos.portfolio.inventory import inventory_for_project as impl

    return impl(session, project_id, commit=commit)


def publication_targets(session: Session, project_id: str) -> list[dict]:
    """The exact publication targets configured for a project, in the one provider vocabulary
    (``aeos.portfolio.providers``): ``{"provider", "root_id", "purpose", "integration_id", "label"}``.

    Only providers of the project's own domain profile are returned (§04); an approval binds these
    targets and the publisher writes only inside them (U-10)."""
    from aeos.models.portfolio import ProjectIntegration
    from aeos.portfolio import providers as pv

    project = session.get(Project, project_id)
    if project is None:
        return []
    rows = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id)))
    out: list[dict] = []
    for purpose in ("docs", "issues"):
        provider = pv.publication_provider(project.domain, purpose)
        match = [r for r in rows if r.domain == project.domain and pv.normalize(r.provider) == provider
                 and r.purpose in (purpose, "default")]
        if not match:
            continue
        r = match[0]
        root = r.root_id
        if not root and provider == pv.GITHUB_ISSUES:
            gh = sorted((x for x in repositories(session, project.id) if x.provider.upper() == "GITHUB"),
                        key=lambda x: (x.role not in ("project", "platform"), x.full_name))
            root = gh[0].full_name if gh else None
        ident = pv.target_identity(provider, root)
        if not ident["root_id"]:
            continue  # no exact identifier -> nothing an approval could bind
        out.append({**ident, "purpose": purpose, "integration_id": r.id, "label": pv.LABELS.get(provider, provider)})
    return out
