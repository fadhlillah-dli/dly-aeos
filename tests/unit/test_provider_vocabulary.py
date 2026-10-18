"""One provider vocabulary across the Integration Registry (seed + API), publisher, interlock allow-lists,
write enablement and Doctor adapter checks (§04, §10, §13; ADR-0020/0073 amendments 2026-10-18)."""

from __future__ import annotations

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.models.portfolio import ProjectIntegration
from aeos.policy import catalog
from aeos.portfolio import providers as pv
from aeos.publisher import providers as pub_providers
from aeos.publisher import targets as pub_targets
from aeos.publisher.providers.atlassian import AtlassianConfig
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project


def test_every_module_uses_the_same_provider_ids():
    assert catalog.PROVIDERS_BY_DOMAIN == pv.WRITE_PROVIDERS
    for domain, providers in pv.WRITE_PROVIDERS.items():
        assert set(providers) <= set(pv.INTEGRATION_PROVIDERS[domain])  # every write target is registrable
    assert set(catalog.PUBLISH_OPERATIONS) | set(catalog.DELIVERY_OPERATIONS) == set(catalog.ALL_PROVIDERS)
    for domain in (Domain.PERSONAL, Domain.OFFICE):
        for purpose in ("docs", "issues"):
            provider = pv.publication_provider(domain, purpose)
            family = pub_targets.PROFILE[domain.value][purpose]
            assert pub_targets.FAMILY_PROVIDER[family] == provider
            assert provider in catalog.PUBLISH_OPERATIONS
    canonical = {p for p in pub_providers.FAMILY_OF if not pv.is_legacy(p)}
    assert canonical == set(catalog.PUBLISH_OPERATIONS)


def test_publisher_operation_kinds_are_known_to_the_interlock():
    planned = {"google-drive": ("create_folder", "create_doc", "update_doc"),
               "github-issues": ("ensure_label", "create_issue"),
               "confluence": ("create_folder", "create_doc", "update_doc", "upload_attachment"),
               "jira": ("create_issue",)}
    for provider, ops in planned.items():
        assert all(catalog.known_operation(provider, op) for op in ops), provider
        assert all(catalog.is_publication_write(provider, op) for op in ops)
    assert not catalog.is_publication_write("github-repo", "push_branch")  # delivery: plan approval + broker


def test_doctor_has_an_adapter_check_for_every_write_provider():
    from aeos.doctor.checks import load_all

    registry = load_all()
    for provider in catalog.ALL_PROVIDERS:
        checks = pv.DOCTOR_CHECKS.get(provider)
        assert checks, provider
        assert all(c in registry for c in checks), (provider, checks)
    from aeos.doctor.checks import adapters

    assert pv.DEFAULT_CREDENTIAL_REFS[pv.GOOGLE_DRIVE] == adapters.GOOGLE_REF  # the ref google-connect writes


def test_legacy_spellings_normalise_and_never_map_to_another_provider():
    assert pv.normalize("GOOGLE_DRIVE") == pv.normalize("google-docs") == pv.normalize("GOOGLE_DOCS") == "google-drive"
    assert pv.normalize("GITHUB_ISSUES") == "github-issues" and pv.normalize("GITHUB") == "github-repo"
    assert pv.normalize("CONFLUENCE") == "confluence" and pv.normalize("notion") == "notion"  # unknown stays unknown
    assert pv.target_identity("google-drive", None) == {"provider": "google-drive", "root_id": "root"}


def test_seed_migrates_legacy_rows_to_the_vocabulary(db, secrets_memory):
    from aeos.portfolio import seed

    seed.run(db)
    db.commit()
    rows = list(db.scalars(select(ProjectIntegration)))
    for r in rows:  # simulate a database written by the earlier build
        r.provider = {"google-drive": "GOOGLE_DRIVE", "github-issues": "GITHUB_ISSUES"}[r.provider]
        if r.provider == "GOOGLE_DRIVE":
            r.credential_ref = pv.LEGACY_GOOGLE_REF
    db.commit()
    res = seed.run(db)
    db.commit()
    assert res["provider_migration"]["renamed"] == 2 and res["provider_migration"]["conflicts"] == []
    by = {r.provider: r for r in db.scalars(select(ProjectIntegration))}
    assert set(by) == {"google-drive", "github-issues"}
    assert by["google-drive"].credential_ref == "personal/google-oauth-token"
    assert by["github-issues"].root_id  # target id kept as it was


def test_integration_api_stores_canonical_ids_and_validates_atlassian_config(admin_client: AuthedClient):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Kantor Docs")
    r = admin_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "CONFLUENCE", "purpose": "docs", "tenant_id": "perusahaan.atlassian.net", "root_id": "123456",
        "credential_ref": "office/atlassian", "config": {"space_id": "ENG", "deployment": "cloud", "enabled": True}})
    assert r.status_code == 200, r.text
    integ = r.json()["integration"]
    assert integ["provider"] == "confluence" and integ["config"]["deployment"] == "CLOUD"
    assert integ["capability_status"]["target"] == {"provider": "confluence", "root_id": "123456"}
    for bad in ({"enabled": "yes"}, {"deployment": "ONPREM"}, {"CONFLUENCE_PROJECTS_PARENT_ID": "9"},
                {"project_key": "ABC"}, {"repo": "o/r"}):
        r = admin_client.put(f"/api/projects/{p['id']}/integrations", json={
            "provider": "confluence", "purpose": "docs", "root_id": "123456", "config": bad})
        assert r.status_code == 422, (bad, r.text)
    r = admin_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "jira", "purpose": "issues", "tenant_id": "perusahaan.atlassian.net", "root_id": "ENG",
        "credential_ref": "office/atlassian", "config": {"deployment": "CLOUD", "enabled": True}})
    assert r.status_code == 200, r.text


def test_put_with_the_canonical_id_renames_a_legacy_row_instead_of_adding_a_second_target(
        personal_client: AuthedClient, db):
    p = create_project(personal_client)
    db.add(ProjectIntegration(project_id=p["id"], domain="PERSONAL", provider="GOOGLE_DRIVE", purpose="docs"))
    db.commit()
    r = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "google-drive", "purpose": "docs", "credential_ref": "personal/google-oauth-token"})
    assert r.status_code == 200, r.text
    db.expire_all()
    rows = list(db.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == p["id"])))
    assert [(x.provider, x.purpose) for x in rows] == [("google-drive", "docs")]


def test_atlassian_adapter_reads_the_registry_shape():
    conf = AtlassianConfig.from_integration("perusahaan.atlassian.net", "123456", {"space_id": "ENG"},
                                            product="confluence")
    assert conf.base_url == "https://perusahaan.atlassian.net" and conf.parent_id == "123456"
    jira = AtlassianConfig.from_integration("perusahaan.atlassian.net", "ENG", {}, product="jira")
    assert jira.project_key == "ENG" and jira.parent_id == ""


def test_publication_targets_use_the_integration_root(db, secrets_memory):
    from aeos.models.portfolio import Project
    from aeos.portfolio import facade

    p = Project(project_key="voc-1", name="Voc", domain="OFFICE")
    db.add(p)
    db.flush()
    db.add_all([ProjectIntegration(project_id=p.id, domain="OFFICE", provider="confluence", purpose="docs",
                                   tenant_id="x.atlassian.net", root_id="777",
                                   config={"space_id": "S", "deployment": "CLOUD", "enabled": True}),
                ProjectIntegration(project_id=p.id, domain="OFFICE", provider="jira", purpose="issues",
                                   tenant_id="x.atlassian.net", root_id="ENG",
                                   config={"deployment": "CLOUD", "enabled": True})])
    db.commit()
    assert {(t["provider"], t["root_id"]) for t in facade.publication_targets(db, p.id)} == {
        ("confluence", "777"), ("jira", "ENG")}
    resolved = {t.provider: t for t in pub_targets.resolve_targets(db, p)}
    assert resolved["confluence"].root_id == "777" and resolved["jira"].root_id == "ENG"
    assert resolved["confluence"].config["atlassian"]["base_url"] == "https://x.atlassian.net"
