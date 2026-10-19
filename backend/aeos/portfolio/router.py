"""Gateway & Portfolio API (§04, §05, §10, §35, §37).

Every project-scoped route depends on ``ActiveDomain`` and loads projects through ``load_project``
so a cross-domain id is a 404 (no metadata leak). Writes require SUPER_ADMIN + CSRF (deps).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, ConfigDict, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.errors import NotFound
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import ProjectIntegration, RepositoryReference
from aeos.portfolio import integrations as integ
from aeos.portfolio import repositories as repos
from aeos.portfolio import search as search_mod
from aeos.portfolio import service

router = APIRouter(tags=["portfolio"])


class RepositoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=32)
    owner: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=110)
    role: str = "project"
    local_path: str | None = Field(default=None, max_length=1024)
    default_branch: str | None = Field(default=None, max_length=200)
    verify: bool = False


class ProjectIn(BaseModel):
    """``domain`` is optional here only so a missing domain gets the explicit DOMAIN_REQUIRED denial."""

    model_config = ConfigDict(extra="forbid")

    domain: str | None = None
    name: str = Field(default="", max_length=400)
    idea: str = Field(default="", max_length=40_000)
    description: str = Field(default="", max_length=10_000)
    project_kind: str = "NEW"
    data_classification: str | None = None
    repository: RepositoryIn | None = None


class LocalPathIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_path: str = Field(min_length=1, max_length=1024)


class InventoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str | None = Field(default=None, max_length=200)


def _freshness() -> dict[str, Any]:
    return {"source": "aeos-portfolio", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/projects")
def list_projects(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                  lifecycle: str | None = Query(None, max_length=16)) -> dict[str, Any]:
    items = [service.project_view(db, p) for p in service.visible_projects(db, principal, domain, lifecycle=lifecycle)]
    return {"items": items, "domain": domain.value, **_freshness()}


@router.post("/api/projects")
def create_project(body: ProjectIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    repo_body = body.repository.model_dump(exclude={"verify"}) if body.repository else None
    project = service.create_project(
        db, principal, active_domain=domain, domain=body.domain, name=body.name, idea=body.idea,
        description=body.description, project_kind=body.project_kind, data_classification=body.data_classification,
        repository=repo_body,
    )
    verification = None
    if body.repository and body.repository.verify:
        repo = repos.primary_repository(db, project.id)
        if repo is not None:
            verification = repos.verify_repository(db, principal, project, repo)
    db.commit()
    return {"project": service.project_view(db, project, detail=True), "verification": verification}


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    return {"project": service.project_view(db, project, detail=True), **_freshness()}


@router.patch("/api/projects/{project_id}")
def patch_project(project_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                  body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    service.update_project(db, principal, project, body)
    db.commit()
    return {"project": service.project_view(db, project, detail=True)}


# ---------------------------------------------------------------- repositories


def _scoped_repo(db: Any, principal: Any, domain: Any, repository_id: str) -> tuple[Any, RepositoryReference]:
    repo = db.get(RepositoryReference, repository_id)
    if repo is None:
        raise NotFound("repository not found")
    project = load_project(db, principal, repo.project_id, domain=domain)
    return project, repo


@router.get("/api/projects/{project_id}/repositories")
def list_repositories(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    rows = (db.query(RepositoryReference).filter(RepositoryReference.project_id == project.id)
            .order_by(RepositoryReference.role, RepositoryReference.full_name).all())  # platform first
    return {"items": [repos.repository_view(r) for r in rows], **_freshness(), "source": "aeos-portfolio (declared + "
            "provider lookups; see last_verified_at)"}


@router.post("/api/projects/{project_id}/repositories")
def add_repository(project_id: str, body: RepositoryIn, principal: AdminPrincipal, domain: ActiveDomain,
                   db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    repo = repos.add_repository(db, principal, project, **body.model_dump(exclude={"verify"}))
    verification = repos.verify_repository(db, principal, project, repo) if body.verify else None
    db.commit()
    return {"repository": repos.repository_view(repo), "verification": verification}


@router.get("/api/repositories/{repository_id}")
def get_repository(repository_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    _project, repo = _scoped_repo(db, principal, domain, repository_id)
    return {"repository": repos.repository_detail(db, repo), **_freshness()}


@router.post("/api/repositories/{repository_id}/verify")
def verify_repository(repository_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project, repo = _scoped_repo(db, principal, domain, repository_id)
    result = repos.verify_repository(db, principal, project, repo)
    db.commit()
    return {"repository": repos.repository_view(repo), "verification": result}


@router.post("/api/repositories/{repository_id}/local-path")
def set_local_path(repository_id: str, body: LocalPathIn, principal: AdminPrincipal, domain: ActiveDomain,
                   db: DB) -> dict[str, Any]:
    project, repo = _scoped_repo(db, principal, domain, repository_id)
    repos.set_local_path(db, principal, project, repo, body.local_path)
    db.commit()
    return {"repository": repos.repository_view(repo)}


@router.post("/api/repositories/{repository_id}/inventory")
def run_inventory(repository_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                  body: InventoryIn | None = None) -> dict[str, Any]:
    project, repo = _scoped_repo(db, principal, domain, repository_id)
    result = repos.inventory_for_repository(db, principal, project, repo, commit=body.commit if body else None)
    db.commit()
    return {"inventory": result, "repository": repos.repository_view(repo)}


@router.get("/api/repositories/{repository_id}/inventory")
def get_inventory(repository_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    _project, repo = _scoped_repo(db, principal, domain, repository_id)
    return {"inventory": repo.inventory or None, "inventory_at": repo.inventory_at.isoformat() if repo.inventory_at
            else None, "commit": repo.inventory_commit, "source": "git ls-tree (read-only)",
            "checked_at": repo.inventory_at.isoformat() if repo.inventory_at else None, "mode": "LIVE"}


# ---------------------------------------------------------------- integrations


@router.get("/api/projects/{project_id}/integrations")
def list_integrations(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    rows = db.query(ProjectIntegration).filter(ProjectIntegration.project_id == project.id).all()
    # credential/root state is re-evaluated on read (configuration only, not persisted)
    views = [integ.live_integration_view(r) for r in rows]
    return {"items": views, "allowed_providers": {k: sorted(v) for k, v in integ.PROVIDERS[project.domain].items()},
            **_freshness(), "source": "integration-registry (configuration only; connectivity via Doctor)"}


@router.put("/api/projects/{project_id}/integrations")
def put_integration(project_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                    body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    row = integ.upsert_integration(db, principal, project, body)
    db.commit()
    return {"integration": integ.integration_view(row)}


# ---------------------------------------------------------------- search


@router.get("/api/search")
def search(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, q: str = Query(..., max_length=200),
           kind: str | None = Query(None, max_length=200), limit: int = Query(20, ge=1, le=100),
           offset: int = Query(0, ge=0, le=10_000),
           project: str | None = Query(None, max_length=36)) -> dict[str, Any]:
    """§35 project-scoped or domain-wide metadata search; ``project`` must be visible (else 404)."""
    project_id = load_project(db, principal, project, domain=domain).id if project else None
    return search_mod.search(db, principal, domain, q=q, kind=kind, limit=limit, offset=offset,
                             project_id=project_id)
