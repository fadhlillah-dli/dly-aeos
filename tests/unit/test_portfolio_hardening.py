"""Gateway & Portfolio hardening found in the completeness review (§04, §09, §35, §37, §50)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.models.inventory import ComponentReference
from aeos.portfolio import facade, gitread
from aeos.portfolio.repositories import is_network_path
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import commit_file, create_project, git, make_repo, needs_git

# ---------------------------------------------------------------- §09 platform key


def test_platform_key_is_reserved_for_the_seed(personal_client: AuthedClient, db, secrets_memory):
    """A human-created "DLY AEOS" project must never occupy ``dly-aeos``; the §09 seed still registers."""
    from aeos.portfolio.seed import run

    p = create_project(personal_client, name="DLY AEOS")
    assert p["project_key"] != facade.PLATFORM_PROJECT_KEY and p["is_platform"] is False
    res = run(db)
    db.commit()
    assert res["project"] == "created" and "warning" not in res
    platform = facade.platform_project(db)
    assert platform is not None and platform.project_key == facade.PLATFORM_PROJECT_KEY
    assert platform.id != p["id"]


def test_seed_refuses_a_malformed_platform_slug(db, secrets_memory, monkeypatch):
    """The configured slug must be an exact owner/name; the seed never guesses or rewrites it (§37)."""
    from aeos.core.config import get_settings
    from aeos.portfolio.seed import run

    monkeypatch.setattr(get_settings(), "platform_repo_slug", "not a/valid/slug")
    res = run(db)
    db.commit()
    assert res["repository"] == "skipped" and "repository_warning" in res
    assert facade.repositories(db, res["project_id"]) == []


# ---------------------------------------------------------------- CLI (domain-first, ASCII)


def _cli(args: list[str]):
    from aeos.portfolio.cli import app

    return CliRunner().invoke(app, args)


def test_cli_requires_explicit_domain_and_hides_other_domain(settings, db, secrets_memory):
    """U-05: `aeos project show/inventory` need --domain; an OFFICE key is "not found" from PERSONAL."""
    from aeos.portfolio.seed import run

    run(db)
    db.commit()
    r = _cli(["show", facade.PLATFORM_PROJECT_KEY])
    assert r.exit_code == 2 and "--domain" in r.output
    r = _cli(["show", facade.PLATFORM_PROJECT_KEY, "--domain", "OFFICE"])
    assert r.exit_code == 1 and "not found" in r.output and "DLY AEOS" not in r.output
    r = _cli(["show", facade.PLATFORM_PROJECT_KEY, "--domain", "PERSONAL"])
    assert r.exit_code == 0, r.output
    view = json.loads(r.output)  # machine-readable: no console soft-wrapping inside the JSON
    assert view["domain"] == "PERSONAL" and view["repositories"][0]["verification_status"] == "UNVERIFIED"
    r.output.encode("ascii")  # ASCII-only output for legacy consoles
    r = _cli(["inventory", facade.PLATFORM_PROJECT_KEY])
    assert r.exit_code == 2


def test_cli_list_output_is_ascii(personal_client: AuthedClient, settings):
    create_project(personal_client, name="Proyek Keuangan \u00e9\u2014\u4e2d\u6587")
    r = _cli(["list", "--domain", "PERSONAL"])
    assert r.exit_code == 0, r.output
    r.output.encode("ascii")
    assert "Proyek Keuangan" in r.output


@needs_git
def test_cli_inventory_reports_errors_cleanly(personal_client: AuthedClient, settings, tmp_path: Path):
    p = create_project(personal_client, name="CLI Inv")
    personal_client.post(f"/api/projects/{p['id']}/repositories",
                         json={"provider": "GITHUB", "owner": "octo", "name": "inv"})
    r = _cli(["inventory", p["project_key"], "--domain", "PERSONAL"])
    assert r.exit_code == 1 and "LOCAL_CHECKOUT_MISSING" in r.output and "Traceback" not in r.output


# ---------------------------------------------------------------- local paths (§50 path rows, Windows)


@pytest.mark.parametrize("value,expected", [
    (r"\\server\share\repo", True),
    ("//server/share/repo", True),
    (r"\\?\UNC\server\share\repo", True),
    (r"\\?\C:\Users\me\repo", False),
    (r"C:\Users\me\repo", False),
    ("/Users/me/repo", False),
])
def test_network_paths_detected(value: str, expected: bool):
    assert is_network_path(value) is expected


# ---------------------------------------------------------------- integrations (§04)


def test_project_credentials_bound_to_own_project(personal_client: AuthedClient):
    a = create_project(personal_client, name="Cred A")
    b = create_project(personal_client, name="Cred B")
    url = f"/api/projects/{a['id']}/integrations"
    r = personal_client.put(url, json={"provider": "GITHUB_ISSUES", "purpose": "issues", "root_id": "octo/a",
                                       "credential_ref": f"projects/{b['project_key']}/github"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "CREDENTIAL_PROJECT_MISMATCH"
    r = personal_client.put(url, json={"provider": "GITHUB_ISSUES", "purpose": "issues", "root_id": "octo/a",
                                       "credential_ref": f"projects/{a['project_key']}/github"})
    assert r.status_code == 200, r.text
    assert r.json()["integration"]["credential_ref"] == f"projects/{a['project_key']}/github"


@pytest.mark.parametrize("key", ["confluence_parent_id", "ROOT_FOLDER", "legacy_alias", "target_page", "pageId"])
def test_config_cannot_hold_a_second_target(admin_client: AuthedClient, key: str):
    """§04: CONFLUENCE_PROJECTS_PARENT_ID lives in root_id; a legacy alias never becomes a second target."""
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Conf Alias")
    r = admin_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "CONFLUENCE", "purpose": "docs", "root_id": "123456", "tenant_id": "corp.atlassian.net",
        "config": {key: "98765"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "TARGET_IN_CONFIG"
    ok = admin_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "CONFLUENCE", "purpose": "docs", "root_id": "123456", "tenant_id": "corp.atlassian.net",
        "config": {"deployment": "CLOUD", "space_key": "ENG"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["integration"]["capability_status"]["atlassian_deployment"] == "CLOUD"
    assert ok.json()["integration"]["capability_status"]["functional"] == "NOT_TESTED"


# ---------------------------------------------------------------- search (§35)


def test_project_scoped_search_and_404_for_other_domain(admin_client: AuthedClient, db):
    admin_client.select_domain(Domain.OFFICE)
    office = create_project(admin_client, domain=Domain.OFFICE, name="Quasar Office")
    admin_client.select_domain(Domain.PERSONAL)
    a = create_project(admin_client, name="Quasar Alpha")
    create_project(admin_client, name="Quasar Beta")
    body = admin_client.get("/api/search", params={"q": "quasar"}).json()
    assert body["counts"]["project"] == 2 and body["project_id"] is None
    scoped = admin_client.get("/api/search", params={"q": "quasar", "project": a["id"]}).json()
    assert scoped["counts"]["project"] == 1 and scoped["items"][0]["id"] == a["id"]
    assert scoped["project_id"] == a["id"]
    r = admin_client.get("/api/search", params={"q": "quasar", "project": office["id"]})
    assert r.status_code == 404 and "Quasar Office" not in r.text


def test_project_scoped_component_not_leaked_across_domains(admin_client: AuthedClient, db):
    """§50 "Viewer mengakses detail/search/export inventory Office": a component scoped to an OFFICE
    project (without a domain key) is not visible, nor counted, in a PERSONAL search."""
    admin_client.select_domain(Domain.OFFICE)
    office = create_project(admin_client, domain=Domain.OFFICE, name="Nebula Office")
    admin_client.select_domain(Domain.PERSONAL)
    personal = create_project(admin_client, name="Nebula Personal")
    db.add_all([
        ComponentReference(component_id="nebula-office-svc", kind="service", name="Nebula Office Service",
                           scope={"project_id": office["id"]}),
        ComponentReference(component_id="nebula-personal-svc", kind="service", name="Nebula Personal Service",
                           scope={"project": personal["project_key"]}),
        ComponentReference(component_id="nebula-odd", kind="service", name="Nebula Domainless",
                           scope={"domain": "OFFICE"}),
    ])
    db.commit()
    body = admin_client.get("/api/search", params={"q": "nebula", "kind": "component"}).json()
    ids = {i["id"] for i in body["items"]}
    assert ids == {"nebula-personal-svc"} and body["counts"]["component"] == 1
    admin_client.select_domain(Domain.OFFICE)
    body = admin_client.get("/api/search", params={"q": "nebula", "kind": "component"}).json()
    assert {i["id"] for i in body["items"]} == {"nebula-office-svc", "nebula-odd"}


# ---------------------------------------------------------------- read-only git (bounded output)


@needs_git
def test_blob_reads_are_bounded_and_complete_only(tmp_path: Path):
    big = "x" * (256 * 1024) + "\n"
    repo = make_repo(tmp_path / "big", {"big.txt": big, "small.txt": "hello\n"})
    head = git(repo, "rev-parse", "HEAD")
    part = gitread.run_git(repo, ["cat-file", "blob", f"{head}:big.txt"], max_output=1024)
    assert part.truncated and len(part.stdout) == 1024 and part.returncode == 0
    assert gitread.show_blob(repo, head, "big.txt", max_bytes=1024, complete_only=True) is None
    assert gitread.show_blob(repo, head, "big.txt", max_bytes=1024) == big.encode()[:1024]
    assert gitread.show_blob(repo, head, "small.txt") == b"hello\n"
    assert gitread.show_blob(repo, head, "missing.txt") is None
    with pytest.raises(Exception, match="invalid repository path"):
        gitread.show_blob(repo, head, "../outside.txt")


@needs_git
def test_inventory_records_head_separately_from_pinned_commit(tmp_path: Path):
    from aeos.portfolio.inventory import inventory_repository

    repo = make_repo(tmp_path / "pin", {"README.md": "# v1\n", "package.json": '{"devDependencies": {"vitest": "1"}}'})
    first = git(repo, "rev-parse", "HEAD")
    second = commit_file(repo, "src/app.py", "print('x')\n")
    at_head = inventory_repository(repo)
    assert at_head["commit"] == second and at_head["head_commit"] == second and at_head["commit_is_head"] is True
    pinned = inventory_repository(repo, commit=first)
    assert pinned["commit"] == first and pinned["head_commit"] == second and pinned["commit_is_head"] is False
    assert "vitest" in pinned["tests"]["frameworks"]
    # the inventory hash describes the commit content, not the checkout state
    assert inventory_repository(repo, commit=first)["inventory_hash"] == pinned["inventory_hash"]


def test_project_key_never_derived_from_a_secret(personal_client: AuthedClient, db):
    """U-13: a credential pasted into the name is redacted before the slug is derived from it."""
    from aeos.models.audit import AuditEvent

    token = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" + "AbCdEfGhIjKlMnOp"
    p = create_project(personal_client, name=f"App {token}")
    assert token not in p["name"] and "[REDACTED]" in p["name"]
    assert token.lower()[4:24] not in p["project_key"] and "z9y8x7" not in p["project_key"]
    db.expire_all()
    for a in db.query(AuditEvent).all():
        assert token not in str(a.details) and token.lower() not in str(a.details)


@needs_git
def test_office_checkout_inside_platform_tree_refused(admin_client: AuthedClient, tmp_path: Path, monkeypatch):
    """§50 "Source Office muncul dalam platform personal working tree": refused before anything is read."""
    from aeos.portfolio import repositories

    platform = make_repo(tmp_path / "platform", {"README.md": "# DLY AEOS\n"})
    nested = make_repo(platform / "vendor" / "office-app", {"app.java": "class A {}\n"},
                       origin="https://bitbucket.org/corp/office-app.git")
    elsewhere = make_repo(tmp_path / "office-clone", {"app.java": "class A {}\n"},
                          origin="https://bitbucket.org/corp/office-app.git")
    monkeypatch.setattr(repositories, "repo_root", lambda: platform)
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office App")
    url = f"/api/projects/{p['id']}/repositories"
    body = {"provider": "BITBUCKET", "owner": "corp", "name": "office-app"}
    r = admin_client.post(url, json={**body, "local_path": str(nested)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_SOURCE_IN_PERSONAL_TREE"
    r = admin_client.post(url, json={**body, "local_path": str(platform)})
    assert r.status_code == 403
    ok = admin_client.post(url, json={**body, "local_path": str(elsewhere)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["repository"]["local_status"] == "OK"
