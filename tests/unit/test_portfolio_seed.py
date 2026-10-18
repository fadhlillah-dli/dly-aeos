"""§09 self-development: DLY AEOS registered as the first PERSONAL project (idempotent seed)."""

from __future__ import annotations

from sqlalchemy import func, select

from aeos.core.enums import Domain
from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference
from aeos.portfolio import facade
from aeos.portfolio.seed import run
from tests.conftest import AuthedClient


def _counts(db) -> tuple[int, int, int]:
    return (db.scalar(select(func.count()).select_from(Project)),
            db.scalar(select(func.count()).select_from(RepositoryReference)),
            db.scalar(select(func.count()).select_from(ProjectIntegration)))


def test_seed_registers_platform_project(db, secrets_memory):
    res = run(db)
    db.commit()
    assert res["project"] == "created" and res["repository"] == "created"
    p = facade.platform_project(db)
    assert p is not None and p.id == res["project_id"]
    assert (p.name, p.project_key, p.domain, p.is_platform, p.project_kind, p.data_classification) == (
        "DLY AEOS", facade.PLATFORM_PROJECT_KEY, "PERSONAL", True, "EXISTING", "INTERNAL")
    repos = facade.repositories(db, p.id)
    assert len(repos) == 1
    r = repos[0]
    assert (r.provider, r.full_name, r.role, r.local_path_ref) == ("GITHUB", "fadhlillah-dli/dly-aeos", "platform",
                                                                     "platform-repo")
    assert r.verification_status == "UNVERIFIED"  # seeding is never verification evidence
    ints = {(i.provider, i.purpose): i for i in db.scalars(select(ProjectIntegration))}
    drive = ints[("google-drive", "docs")]
    assert drive.root_id is None and drive.capability_status["configuration"] == "MISSING"
    assert drive.credential_ref == "personal/google-oauth-token"  # the ref google-connect actually writes
    assert ints[("github-issues", "issues")].root_id == "fadhlillah-dli/dly-aeos"
    assert all(i.last_verified_at is None for i in ints.values())


def test_seed_is_idempotent(db, secrets_memory):
    first = run(db)
    db.commit()
    counts = _counts(db)
    second = run(db)
    db.commit()
    assert second["project"] == "exists" and second["project_id"] == first["project_id"]
    assert second["repository_id"] == first["repository_id"]
    assert second["integrations"] == {"google-drive/docs": "exists", "github-issues/issues": "exists"}
    assert _counts(db) == counts


def test_seed_never_rewrites_an_existing_domain(db, secrets_memory):
    db.add(Project(project_key=facade.PLATFORM_PROJECT_KEY, name="Imposter", domain="OFFICE", is_platform=False))
    db.commit()
    res = run(db)
    db.commit()
    assert "warning" in res
    p = db.scalar(select(Project).where(Project.project_key == facade.PLATFORM_PROJECT_KEY))
    assert p.domain == "OFFICE" and p.name == "Imposter"
    assert facade.platform_project(db) is None


def test_seeded_project_visible_in_personal_only(admin_client: AuthedClient, db, secrets_memory):
    run(db)
    db.commit()
    admin_client.select_domain(Domain.PERSONAL)
    items = admin_client.get("/api/projects").json()["items"]
    assert items[0]["is_platform"] is True and items[0]["repositories"][0]["role"] == "platform"
    assert items[0]["repositories"][0]["local_path"].startswith("platform-repo")
    admin_client.select_domain(Domain.OFFICE)
    assert admin_client.get("/api/projects").json()["items"] == []
