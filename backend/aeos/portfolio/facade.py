"""Cross-module contract for projects (§04, §13). Owned by ``aeos.portfolio``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def on_classification_decision_inactive(session: Session, decision: Any) -> dict[str, Any] | None:
    """Additive (ADR-0133): the decision registry calls this when a POLICY decision on
    ``project_classification/<project id>`` stops being ACTIVE. A revoked or expired lowering decision that still
    backs the project's class restores the class it lowered from (fail closed); a superseded one, or one a later
    raise already ended, changes nothing. Returns what was restored, or None."""
    from aeos.portfolio.reclassify import on_decision_inactive as impl

    return impl(session, decision)


def inventory_for_project(session: Session, project_id: str, *, commit: str | None = None) -> dict:
    """Read-only inventory of the project's registered primary repository (ADR-0022 §6) for
    ``PlanningContext.repo_inventory``. Raises ``NotFound``/``Blocked`` (no repository/checkout) and
    ``PolicyDenied`` when the checkout crosses the domain boundary. Never guesses a path."""
    from aeos.portfolio.inventory import inventory_for_project as impl

    return impl(session, project_id, commit=commit)


def primary_repository(session: Session, project_id: str) -> RepositoryReference | None:
    """The project's primary repository (platform repository first, then one with a local checkout)."""
    from aeos.portfolio.repositories import primary_repository as impl

    return impl(session, project_id)


def repository_checkout(session: Session, project_id: str, *, principal: Any = None,
                        action: str) -> tuple[Project, RepositoryReference, Path]:
    """Project, primary repository and its registered local checkout for an operation that reads repository
    content (additive, wave 7: the verifier's project test baseline). Applies the same checks as the inventory:
    the project is writable, a repository and a local checkout are registered (``Blocked``), and the checkout does
    not cross the domain boundary (``PolicyDenied``). Never guesses a path."""
    from aeos.core.errors import Blocked, NotFound
    from aeos.portfolio import repositories
    from aeos.portfolio.service import require_writable

    project = session.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    require_writable(project)
    repo = repositories.primary_repository(session, project.id)
    if repo is None:
        raise Blocked("no repository is registered for this project", code="REPOSITORY_MISSING")
    path = repositories.resolve_local_path(repo)
    if path is None:
        raise Blocked("no local checkout is registered for this repository", code="LOCAL_CHECKOUT_MISSING")
    repositories.require_checkout_in_domain(session, principal, project, repo, path, action=action)
    return project, repo, path


# ---- read-only checkout access at an exact commit (docs_engine §29 diagram references) -------------------------


def project_checkout(session: Session, project: Project, *, action: str) -> tuple[RepositoryReference | None,
                                                                                    Path | None]:
    """(primary repository, its registered local checkout) of *project*, after the use-time domain checks
    (``PolicyDenied`` when the checkout overlaps the other domain or lives on its provider). ``(None, None)`` = no
    repository; ``(repo, None)`` = no local checkout registered. Never guesses a path."""
    from aeos.portfolio import repositories as repos

    repo = repos.primary_repository(session, project.id)
    if repo is None:
        return None, None
    path = repos.resolve_local_path(repo)
    if path is None:
        return repo, None
    repos.require_checkout_in_domain(session, None, project, repo, path, action=action)
    return repo, path


def git_toplevel(path: Path) -> Path:
    from aeos.portfolio import gitread

    return gitread.toplevel(path)


def git_resolve_commit(repo: Path, commitish: str = "HEAD") -> str:
    """Full SHA of *commitish* in *repo*; ``ValidationFailed`` (COMMIT_NOT_FOUND / INVALID_COMMIT) otherwise."""
    from aeos.portfolio import gitread

    return gitread.resolve_commit(repo, commitish)


def git_list_files(repo: Path, commit: str, *, max_entries: int = 100_000) -> tuple[list[str], bool]:
    """Regular files of *commit* (``git ls-tree``; no symlinks/submodules, sensitive names such as ``.env`` or keys
    excluded) and whether the listing was truncated."""
    from aeos.portfolio import gitread
    from aeos.portfolio.inventory import SENSITIVE_NAMES

    entries, truncated = gitread.ls_tree(repo, commit, max_entries=max_entries)
    files = [e.path for e in entries if e.kind == "blob" and not e.is_symlink and not SENSITIVE_NAMES.search(e.path)]
    return sorted(files), truncated


def git_read_file(repo: Path, commit: str, path: str, *, max_bytes: int = 256 * 1024) -> bytes | None:
    """Content of *path* at *commit* from the object database (never the working tree, no filters); ``None`` when
    missing, a sensitive name, or larger than *max_bytes*."""
    from aeos.portfolio import gitread
    from aeos.portfolio.inventory import SENSITIVE_NAMES

    if SENSITIVE_NAMES.search(path):
        return None
    return gitread.show_blob(repo, commit, path, max_bytes=max_bytes, complete_only=True)


def classify_repo_path(path: str) -> dict[str, str | None]:
    """The portfolio inventory's classification of one repository path: ``ci`` (e.g. GitHub Actions), ``deploy``
    (container/compose/terraform/...) and ``contract`` (openapi/prisma/db-migrations/...) labels, or None."""
    from aeos.portfolio.inventory import CI_PATTERNS, CONTRACT_PATTERNS, DEPLOY_PATTERNS, _match

    return {"ci": _match(CI_PATTERNS, path), "deploy": _match(DEPLOY_PATTERNS, path),
            "contract": _match(CONTRACT_PATTERNS, path)}


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
        if provider is None:
            continue  # no publication provider for this purpose in the project's domain profile
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


def record_integration_probe(session: Session, *, domain: str, providers: tuple[str, ...], root_id: str,
                             connectivity: str, functional: str = "NOT_TESTED", observer: str,
                             evidence_ref: str | None = None) -> list[str]:
    """A trusted probe (doctor check, publisher capability probe) observed the exact target
    ``providers``/``root_id`` in ``domain``: record it on every integration bound to that target
    (§10 Integration Registry health). Returns the updated integration ids."""
    from aeos.portfolio.integrations import integrations_for_target, mark_verified

    updated = []
    for integ in integrations_for_target(session, domain=domain, providers=providers, root_id=root_id):
        if mark_verified(session, integ.id, connectivity=connectivity, functional=functional, observer=observer,
                         evidence_ref=evidence_ref) is not None:
            updated.append(integ.id)
    return updated


def record_publication_success(session: Session, integration_id: str, *, root_id: str, observer: str,
                               evidence_ref: str) -> bool:
    """A LIVE publication write succeeded against ``root_id``: functional proof for the integration, but only
    when that is still the integration's CURRENT exact target (a changed target proves nothing about the new
    one). Returns whether the proof was recorded."""
    from aeos.models.portfolio import ProjectIntegration
    from aeos.portfolio import providers as pv
    from aeos.portfolio.integrations import mark_verified

    integ = session.get(ProjectIntegration, integration_id)
    if integ is None or not root_id:
        return False
    if pv.target_identity(integ.provider, integ.root_id)["root_id"] != root_id:
        return False
    return mark_verified(session, integ.id, connectivity="CONNECTED", functional="VERIFIED", observer=observer,
                         evidence_ref=evidence_ref) is not None
