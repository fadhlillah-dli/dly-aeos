"""Register the DLY AEOS self-build project (§09, U-15, U-18). Idempotent.

* name "DLY AEOS", key ``dly-aeos``, domain PERSONAL (the owner's recorded choice in §09 — not an
  inference), ``is_platform``, kind EXISTING, classification INTERNAL;
* platform repository GITHUB ``settings.platform_repo_slug`` (role platform), UNVERIFIED until a
  provider lookup succeeds; local checkout is the logical ``platform-repo`` resolved at use time;
* docs integration GOOGLE_DRIVE (root folder pending owner OAuth) and issues integration
  GITHUB_ISSUES. Nothing is published, verified or connected by seeding.

Re-running never changes an existing domain (immutable) and never overwrites target IDs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import get_settings
from aeos.core.enums import DataClassification, Domain
from aeos.events import service as events
from aeos.models.portfolio import Project, RepositoryReference
from aeos.portfolio.facade import PLATFORM_PROJECT_KEY
from aeos.portfolio.integrations import ensure_integration
from aeos.portfolio.repositories import PLATFORM_LOCAL_REF

PLATFORM_NAME = "DLY AEOS"
PLATFORM_DESCRIPTION = (
    "DLY AI Engineering Operating System — local-first control plane. Proyek pertama workforce adalah "
    "peningkatan DLY AEOS sendiri (master §09): trusted controller terpisah dari candidate workspace."
)
PLATFORM_IDEA = (
    "Upgrade DLY AEOS secara incremental dari source existing yang sudah diinventarisasi: Doctor + Readiness, "
    "Gateway domain-first, immutable planning/approval, publisher PERSONAL, satu sandboxed worker loop, "
    "verifikasi/review, lalu modul berikutnya."
)


def run(session: Session) -> dict[str, Any]:
    settings = get_settings()
    slug = settings.platform_repo_slug
    owner, _, name = slug.partition("/")
    result: dict[str, Any] = {"project_key": PLATFORM_PROJECT_KEY}

    project = session.scalar(select(Project).where(Project.project_key == PLATFORM_PROJECT_KEY))
    if project is None:
        project = Project(
            project_key=PLATFORM_PROJECT_KEY, name=PLATFORM_NAME, domain=Domain.PERSONAL.value, lifecycle="ACTIVE",
            data_classification=DataClassification.INTERNAL.value, description=PLATFORM_DESCRIPTION,
            idea=PLATFORM_IDEA, is_platform=True, project_kind="EXISTING", mode="LIVE", input_findings=[],
        )
        session.add(project)
        session.flush()
        result["project"] = "created"
        audit.record(session, actor_type="system", actor_id="seed", action="portfolio.project.seed",
                     target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                     details={"project_key": PLATFORM_PROJECT_KEY, "is_platform": True})
        events.emit(session, source="portfolio", kind="portfolio.project.created", entity_type="project",
                    entity_id=project.id, project_id=project.id, domain=project.domain,
                    payload={"name": PLATFORM_NAME, "project_key": PLATFORM_PROJECT_KEY, "seed": True})
    else:
        result["project"] = "exists"
        if project.domain != Domain.PERSONAL.value or not project.is_platform:
            # Never "fix" a domain by seeding: the domain is immutable and only a human migration moves it.
            result["warning"] = (f"project {PLATFORM_PROJECT_KEY} exists with domain={project.domain} "
                                 f"is_platform={project.is_platform}; left unchanged")
            result["project_id"] = project.id
            return result
    result["project_id"] = project.id

    repo = session.scalar(select(RepositoryReference).where(
        RepositoryReference.project_id == project.id, RepositoryReference.role == "platform"))
    if repo is None:
        repo = RepositoryReference(
            project_id=project.id, provider="GITHUB", full_name=slug, url=f"https://github.com/{slug}",
            default_branch="main", role="platform", local_path_ref=PLATFORM_LOCAL_REF,
            verification_status="UNVERIFIED",
            verification_reason="seeded from configuration (platform_repo_slug); verify with the provider",
            details={"url_source": "declared", "declared_owner": owner, "declared_name": name, "seed": True},
        )
        session.add(repo)
        session.flush()
        result["repository"] = "created"
    else:
        result["repository"] = "exists"
        if repo.full_name.lower() != slug.lower():
            result["repository_warning"] = (f"platform repository is {repo.full_name}, configuration says {slug}; "
                                            "left unchanged (identity is never rewritten by a seed)")
    result["repository_id"] = repo.id

    _docs, docs_created = ensure_integration(
        session, project, provider="GOOGLE_DRIVE", purpose="docs", root_id=None,
        credential_ref="personal/google-oauth", config={"pending": "owner OAuth consent (aeos publish google-connect)"},
    )
    _issues, issues_created = ensure_integration(
        session, project, provider="GITHUB_ISSUES", purpose="issues", root_id=slug, credential_ref="personal/github",
    )
    result["integrations"] = {"GOOGLE_DRIVE/docs": "created" if docs_created else "exists",
                              "GITHUB_ISSUES/issues": "created" if issues_created else "exists"}
    result["verification_status"] = repo.verification_status
    return result
