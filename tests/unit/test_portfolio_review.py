"""Completeness-review regressions for Gateway & Portfolio / Knowledge.

* local checkouts never cross the PERSONAL/OFFICE boundary (AGENTS.md invariant 9 "Office data never
  flows into Personal views"; §50 "Source Office muncul dalam platform personal working tree" ->
  stopped/reviewed): overlapping checkouts are refused at registration, and repository content is
  not read (inventory, context packs) when the checkout's origin lives on the other domain's provider;
* ARCHIVED/RETIRED projects are read-only (lifecycle without deletes; RETIRED terminal);
* duplicate repository detection is exact (no SQL LIKE wildcards);
* ``inventory_for_project`` gives planning a project-level inventory for PlanningContext.repo_inventory;
* the context-pack default forbidden scope covers common credential files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import Blocked, NotFound, PolicyDenied
from aeos.models.audit import AuditEvent
from aeos.models.portfolio import RepositoryReference
from aeos.models.research import ContextPack, MemoryRecord
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import (
    create_project,
    git,
    make_repo,
    needs_git,
    record_retirement_decision,
)


def _denied(db, action: str, code: str) -> list[AuditEvent]:
    db.expire_all()
    rows = db.scalars(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.result == "DENIED"))
    return [a for a in rows if (a.details or {}).get("code") == code]


def _add_repo(client: AuthedClient, pid: str, provider: str, owner: str, name: str, path: Path | None = None):
    body: dict = {"provider": provider, "owner": owner, "name": name}
    if path is not None:
        body["local_path"] = str(path)
    return client.post(f"/api/projects/{pid}/repositories", json=body)


# ---------------------------------------------------------------- domain boundary of checkouts


@needs_git
def test_checkout_cannot_be_shared_across_domains(admin_client: AuthedClient, db, tmp_path: Path):
    """An OFFICE project cannot register (or nest inside) a checkout already registered in PERSONAL."""
    personal_repo = make_repo(tmp_path / "personal-app", {"README.md": "hi\n"},
                              origin="https://github.com/octo/app.git")
    admin_client.select_domain(Domain.PERSONAL)
    p = create_project(admin_client, name="Rahasia Pribadi")
    assert _add_repo(admin_client, p["id"], "GITHUB", "octo", "app", personal_repo).status_code == 200

    admin_client.select_domain(Domain.OFFICE)
    o = create_project(admin_client, domain=Domain.OFFICE, name="Kantor")
    r = _add_repo(admin_client, o["id"], "BITBUCKET", "company", "svc", personal_repo)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CHECKOUT_CROSS_DOMAIN"
    # the refusal reveals nothing about the other domain's project
    assert "Rahasia Pribadi" not in r.text and p["project_key"] not in r.text and p["id"] not in r.text
    assert _denied(db, "portfolio.repository.add", "CHECKOUT_CROSS_DOMAIN")

    nested = make_repo(personal_repo / "vendor" / "svc", {"a.txt": "x\n"}, origin="https://bitbucket.org/company/svc.git")
    r = _add_repo(admin_client, o["id"], "BITBUCKET", "company", "svc", nested)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CHECKOUT_CROSS_DOMAIN"

    # registering the repository without a path works; pointing it at the personal checkout later does not
    r = _add_repo(admin_client, o["id"], "BITBUCKET", "company", "svc")
    assert r.status_code == 200, r.text
    rid = r.json()["repository"]["id"]
    r = admin_client.post(f"/api/repositories/{rid}/local-path", json={"local_path": str(personal_repo)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CHECKOUT_CROSS_DOMAIN"
    assert _denied(db, "portfolio.repository.local_path", "CHECKOUT_CROSS_DOMAIN")
    db.expire_all()
    assert db.get(RepositoryReference, rid).local_path_ref is None


@needs_git
def test_personal_checkout_with_office_origin_is_not_read(personal_client: AuthedClient, db, tmp_path: Path,
                                                         secrets_memory):
    """The checkout's origin is re-checked at use time: once it points at Bitbucket (OFFICE code
    provider) a PERSONAL inventory/context pack refuses to read it, and the refusal is audited."""
    repo = make_repo(tmp_path / "mixed", {"src/a.py": "def office_thing():\n    return 1\n"},
                     origin="https://github.com/octo/mixed.git")
    p = create_project(personal_client, name="Mixed")
    r = _add_repo(personal_client, p["id"], "GITHUB", "octo", "mixed", repo)
    assert r.status_code == 200, r.text
    rid = r.json()["repository"]["id"]
    ok = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"query": ["office_thing"]})
    assert ok.status_code == 200, ok.text

    git(repo, "remote", "set-url", "origin", "git@bitbucket.org:company/svc.git")
    r = personal_client.post(f"/api/repositories/{rid}/inventory", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CHECKOUT_OTHER_DOMAIN_ORIGIN"
    r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"query": ["office_thing"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CHECKOUT_OTHER_DOMAIN_ORIGIN"
    assert "return 1" not in r.text
    assert _denied(db, "portfolio.repository.inventory", "CHECKOUT_OTHER_DOMAIN_ORIGIN")
    assert _denied(db, "knowledge.context_pack.build", "CHECKOUT_OTHER_DOMAIN_ORIGIN")
    db.expire_all()
    assert not db.get(RepositoryReference, rid).inventory
    assert len(list(db.scalars(select(ContextPack).where(ContextPack.project_id == p["id"])))) == 1

    # a fresh observation records the conflict instead of reporting the checkout as OK
    with respx.mock() as mock:
        mock.get("https://api.github.com/repos/octo/mixed").respond(404, json={"message": "Not Found"})
        view = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert view["local_status"] == "MISMATCH"
    assert view["local"]["domain_conflict"] is True and "OFFICE" in view["local"]["reason"]


def test_origin_domain_conflict_detection():
    from aeos.portfolio.repositories import origin_domain_conflict

    assert origin_domain_conflict("git@bitbucket.org:company/svc.git", "PERSONAL")
    assert origin_domain_conflict("https://bitbucket.company.example/scm/team/svc.git", "PERSONAL")
    assert origin_domain_conflict("https://github.com/octo/app.git", "PERSONAL") is None
    assert origin_domain_conflict("https://github.com/octo/app.git", "OFFICE")
    assert origin_domain_conflict("git@bitbucket.org:company/svc.git", "OFFICE") is None
    assert origin_domain_conflict(None, "PERSONAL") is None
    assert origin_domain_conflict("not a url", "OFFICE") is None


def test_overlap_is_component_wise(tmp_path: Path):
    from aeos.portfolio.repositories import _overlaps

    a, sibling, child = tmp_path / "app", tmp_path / "app-2", tmp_path / "app" / "sub"
    assert _overlaps(a, child) and _overlaps(child, a) and _overlaps(a, a)
    assert not _overlaps(a, sibling)  # a shared string prefix is not containment
    if sys.platform in ("win32", "darwin"):  # case-insensitive file systems
        assert _overlaps(Path(str(a).upper()), child)


def test_system_caller_gets_policy_denied_without_principal(personal_client: AuthedClient, db, tmp_path: Path,
                                                            monkeypatch):
    from aeos.models.portfolio import Project
    from aeos.portfolio import gitread, repositories

    p = create_project(personal_client, name="Cli Guard")
    project = db.get(Project, p["id"])
    repo = RepositoryReference(project_id=project.id, provider="GITHUB", full_name="octo/cli", role="project")
    db.add(repo)
    db.flush()
    monkeypatch.setattr(gitread, "remote_url", lambda path, name="origin": "https://bitbucket.org/c/x.git")
    with pytest.raises(PolicyDenied) as exc:
        repositories.require_checkout_in_domain(db, None, project, repo, tmp_path, action="test")
    assert exc.value.code == "CHECKOUT_OTHER_DOMAIN_ORIGIN"
    db.rollback()


# ---------------------------------------------------------------- read-only lifecycles


@needs_git
def test_archived_and_retired_projects_are_read_only(personal_client: AuthedClient, db, tmp_path: Path):
    p = create_project(personal_client, name="Frozen")
    pid = p["id"]
    repo_dir = make_repo(tmp_path / "frozen", {"README.md": "x\n"}, origin="https://github.com/octo/frozen.git")
    r = _add_repo(personal_client, pid, "GITHUB", "octo", "frozen", repo_dir)
    assert r.status_code == 200, r.text
    rid = r.json()["repository"]["id"]
    mem = personal_client.post(f"/api/projects/{pid}/memory",
                               json={"content": "Pemilik memilih SQLite untuk V1", "source_type": "human_statement"})
    assert mem.status_code == 200, mem.text
    mid = mem.json()["memory"]["id"]

    assert personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "ARCHIVED",
                                                              "lifecycle_reason": "jeda"}).status_code == 200
    writes = {
        "add repository": lambda: _add_repo(personal_client, pid, "GITHUB", "octo", "other"),
        "local path": lambda: personal_client.post(f"/api/repositories/{rid}/local-path",
                                                   json={"local_path": str(repo_dir)}),
        "verify": lambda: personal_client.post(f"/api/repositories/{rid}/verify"),
        "inventory": lambda: personal_client.post(f"/api/repositories/{rid}/inventory", json={}),
        "integration": lambda: personal_client.put(f"/api/projects/{pid}/integrations", json={
            "provider": "GITHUB_ISSUES", "purpose": "issues", "root_id": "octo/frozen"}),
        "upload": lambda: personal_client.post(f"/api/projects/{pid}/attachments",
                                               files={"file": ("a.md", b"# a\n", "text/markdown")}),
        "context pack": lambda: personal_client.post(f"/api/projects/{pid}/context-packs", json={}),
        "memory propose": lambda: personal_client.post(f"/api/projects/{pid}/memory", json={
            "content": "Fakta lain", "source_type": "human_statement"}),
        "memory accept": lambda: personal_client.post(f"/api/memory/{mid}/accept", json={}),
        "edit idea": lambda: personal_client.patch(f"/api/projects/{pid}", json={"idea": "baru"}),
    }
    for label, call in writes.items():
        r = call()
        assert r.status_code == 409, f"{label}: {r.status_code} {r.text}"
        assert r.json()["error"]["code"] == "PROJECT_READ_ONLY", label
    # reads stay available; rejecting a proposal never grants anything, so it is still possible
    for url in (f"/api/projects/{pid}", f"/api/projects/{pid}/repositories", f"/api/projects/{pid}/attachments",
                f"/api/projects/{pid}/memory", f"/api/projects/{pid}/context-packs"):
        assert personal_client.get(url).status_code == 200, url
    assert personal_client.post(f"/api/memory/{mid}/reject", json={"rationale": "arsip"}).status_code == 200
    db.expire_all()
    assert db.get(MemoryRecord, mid).status == "REJECTED"

    # reactivation is a lifecycle change and re-enables writes
    assert personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "ACTIVE"}).status_code == 200
    assert writes["integration"]().status_code == 200

    # RETIRED is terminal and stays read-only
    assert personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRING"}).status_code == 200
    record_retirement_decision(personal_client, pid)  # §33 owner decision bound to the checklist
    assert personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRED"}).status_code == 200
    for label in ("add repository", "upload", "edit idea"):
        r = writes[label]()
        assert r.status_code == 409 and r.json()["error"]["code"] == "PROJECT_READ_ONLY", label
    assert personal_client.get(f"/api/projects/{pid}").json()["project"]["lifecycle"] == "RETIRED"


# ---------------------------------------------------------------- exact duplicates


def test_repository_duplicate_check_is_exact(personal_client: AuthedClient):
    """'_' in a repository name is a literal character, not a LIKE wildcard."""
    p = create_project(personal_client, name="Dupes")
    assert _add_repo(personal_client, p["id"], "GITHUB", "octo", "aXb").status_code == 200
    r = _add_repo(personal_client, p["id"], "GITHUB", "octo", "a_b")
    assert r.status_code == 200, r.text
    r = _add_repo(personal_client, p["id"], "GITHUB", "Octo", "A_B")
    assert r.status_code == 409 and r.json()["error"]["code"] == "REPOSITORY_EXISTS"


# ---------------------------------------------------------------- planning inventory


@needs_git
def test_inventory_for_project_for_planning(personal_client: AuthedClient, db, tmp_path: Path):
    """§05 step 6: planning gets the project's inventory at a known commit without guessing a path."""
    from aeos.portfolio.inventory import inventory_for_project

    p = create_project(personal_client, name="Plan Inventory")
    with pytest.raises(NotFound):
        inventory_for_project(db, "00000000-0000-0000-0000-000000000000")
    with pytest.raises(Blocked) as exc:
        inventory_for_project(db, p["id"])
    assert exc.value.code == "REPOSITORY_MISSING"
    assert _add_repo(personal_client, p["id"], "GITHUB", "octo", "nolocal").status_code == 200
    with pytest.raises(Blocked) as exc:
        inventory_for_project(db, p["id"])
    assert exc.value.code == "LOCAL_CHECKOUT_MISSING"
    db.rollback()

    repo_dir = make_repo(tmp_path / "plan-inv", {"pyproject.toml": "[project]\nname = 'x'\n",
                                                 "tests/test_a.py": "def test_a():\n    pass\n"},
                         origin="https://github.com/octo/plan-inv.git")
    p2 = create_project(personal_client, name="Plan Inventory 2")
    r = _add_repo(personal_client, p2["id"], "GITHUB", "octo", "plan-inv", repo_dir)
    rid = r.json()["repository"]["id"]
    inv = inventory_for_project(db, p2["id"])
    db.commit()
    assert inv["project"] == {"id": p2["id"], "project_key": p2["project_key"], "domain": "PERSONAL"}
    assert inv["repository"]["verification_status"] == "UNVERIFIED"
    assert inv["commit"] == git(repo_dir, "rev-parse", "HEAD")
    assert inv["tests"]["baseline"]["status"] == "NOT_RUN" and inv["scaffolded"] is False
    db.expire_all()
    stored = db.get(RepositoryReference, rid)
    assert stored.inventory_commit == inv["commit"] and "project" not in stored.inventory


# ---------------------------------------------------------------- forbidden scope


@needs_git
def test_pack_default_forbidden_covers_common_credential_files(personal_client: AuthedClient, tmp_path: Path):
    files = {
        "src/app.py": "def needle_fn():\n    return 1\n",
        "config/secrets/prod.yaml": "needle_fn: 1\n",
        "infra/terraform.tfstate": "needle_fn\n",
        ".npmrc": "needle_fn\n",
        "deploy/.aws/config": "needle_fn\n",
        "home/.ssh/config": "needle_fn\n",
    }
    repo = make_repo(tmp_path / "forbid", files, origin="https://github.com/octo/forbid.git")
    p = create_project(personal_client, name="Forbidden Scope")
    assert _add_repo(personal_client, p["id"], "GITHUB", "octo", "forbid", repo).status_code == 200
    r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"query": ["needle_fn"], "paths": ["*"]})
    assert r.status_code == 200, r.text
    pack = r.json()["context_pack"]
    paths = {i["provenance"].get("path") for i in pack["items"] if i["kind"] in {"symbol", "repo_file", "code_match"}}
    assert "src/app.py" in paths
    assert not paths & (set(files) - {"src/app.py"})


# ---------------------------------------------------------------- CLI remedy text


def test_cli_empty_office_list_does_not_suggest_the_personal_seed(settings, db):
    """The self project is PERSONAL (§09): an empty OFFICE list points at a human-created project."""
    from typer.testing import CliRunner

    from aeos.portfolio.cli import app

    r = CliRunner().invoke(app, ["list", "--domain", "OFFICE"])
    assert r.exit_code == 0, r.output
    assert "aeos seed" not in r.output and "Proyek baru" in r.output
    r.output.encode("ascii")
    r = CliRunner().invoke(app, ["list", "--domain", "PERSONAL"])
    assert r.exit_code == 0 and "aeos seed" in r.output


# ---------------------------------------------------------------- integration state freshness


def test_project_cards_reflect_a_credential_stored_after_the_put(personal_client: AuthedClient, secrets_memory):
    """§35 freshness: the card's docs/issues state is computed on read, not frozen at the last PUT;
    it still never claims VERIFIED without a functional proof (last_verified_at)."""
    p = create_project(personal_client, name="Fresh Integrations")
    r = personal_client.put(f"/api/projects/{p['id']}/integrations", json={
        "provider": "GITHUB_ISSUES", "purpose": "issues", "root_id": "octo/fresh", "credential_ref": "personal/github"})
    assert r.status_code == 200, r.text
    assert r.json()["integration"]["capability_status"]["configuration"] == "MISSING"

    def card() -> dict:
        items = personal_client.get("/api/projects").json()["items"]
        return next(i for i in items if i["id"] == p["id"])

    assert card()["integration_state"]["issues"] == "MISSING"
    secrets_memory.put("personal/github", "ghp_" + "Z" * 36, actor="test")
    c = card()
    assert c["integration_state"]["issues"] == "CONFIGURED"
    assert c["integrations"][0]["capability_status"]["credential"]["status"] == "CONFIGURED"
    assert c["integrations"][0]["capability_status"]["connectivity"] == "NOT_CHECKED"
    assert "Z" * 36 not in str(c)
    detail = personal_client.get(f"/api/projects/{p['id']}").json()["project"]
    assert detail["integration_state"]["issues"] == "CONFIGURED"
