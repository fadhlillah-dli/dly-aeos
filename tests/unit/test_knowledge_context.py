"""ContextPack (§24): known commit, provenance, budget, forbidden scope, staleness, DATA labelling."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.research import ResearchEntry
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import commit_file, create_project, git, make_repo, needs_git

TOKEN = "ghp_" + "K" * 36
REPO_FILES = {
    "src/billing.py": "import os\n\n\ndef compute_total(items):\n    \"\"\"Sum line items.\"\"\"\n"
                      "    return sum(i.price for i in items)\n\n\nclass InvoiceBuilder:\n    pass\n",
    "src/api.ts": "export function computeTotalHandler(req) {\n  return compute_total(req.items)\n}\n",
    "src/secret_config.py": "compute_total = None  # secret config\n",
    "src/leaky.py": f"# compute_total uses GITHUB_TOKEN={TOKEN}\n",
    ".env": "compute_total=1\nAPI_KEY=supersecretvalue123\n",
    "docs/README.md": "compute_total documentation\n",
    "big/blob.txt": "compute_total\n" + ("filler line\n" * 4000),
}


def _setup(client: AuthedClient, tmp_path: Path) -> tuple[dict, str, Path]:
    repo_dir = make_repo(tmp_path / "ctx", REPO_FILES, origin="https://github.com/octo/ctx.git",
                         symlinks={"src/escape.py": "../../../../etc/passwd"})
    p = create_project(client, name="Context Demo")
    r = client.post(f"/api/projects/{p['id']}/repositories", json={
        "provider": "GITHUB", "owner": "octo", "name": "ctx", "local_path": str(repo_dir)})
    assert r.status_code == 200, r.text
    return p, r.json()["repository"]["id"], repo_dir


def _build(client: AuthedClient, pid: str, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/context-packs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["context_pack"]


@needs_git
def test_pack_items_have_provenance_and_hash(personal_client: AuthedClient, tmp_path: Path):
    p, _rid, repo_dir = _setup(personal_client, tmp_path)
    head = git(repo_dir, "rev-parse", "HEAD")
    pack = _build(personal_client, p["id"], query=["compute_total"], forbidden_paths=["src/secret_*"])
    assert pack["base_commit"] == head
    assert pack["pack_hash"].startswith("sha256:") and pack["freshness"] == "CURRENT"
    assert pack["token_estimate"] <= pack["token_budget"]
    for item in pack["items"]:
        assert {"kind", "ref", "provenance", "classification", "hash", "tokens", "trust"} <= set(item)
        assert item["hash"].startswith("sha256:")
    symbols = [i for i in pack["items"] if i["kind"] == "symbol"]
    assert symbols and symbols[0]["provenance"]["path"] == "src/billing.py"
    assert symbols[0]["provenance"]["commit"] == head and symbols[0]["trust"] == "REPO_DATA"
    assert "def compute_total" in symbols[0]["content"]
    refs = " ".join(i["ref"] for i in pack["items"])
    assert ".env" not in refs and "secret_config" not in refs and "escape.py" not in refs
    assert "supersecretvalue" not in str(pack) and "/etc/passwd" not in str(pack)
    policy = [i for i in pack["items"] if i["kind"] == "policy"]
    assert policy and all(i["trust"] == "POLICY_READ_ONLY" and i["provenance"]["sha256"] for i in policy)
    assert any("MCI reference status" in w for w in pack["warnings"])
    assert pack["request"]["skipped"]["symlinks"] == 1


@needs_git
def test_secrets_in_repo_content_are_redacted(personal_client: AuthedClient, tmp_path: Path):
    p, _rid, _repo = _setup(personal_client, tmp_path)
    pack = _build(personal_client, p["id"], query=["GITHUB_TOKEN"], include=["code_match"])
    leaky = [i for i in pack["items"] if "leaky.py" in i["ref"]]
    assert leaky and leaky[0]["redacted"] is True and TOKEN not in str(pack)


@needs_git
def test_budget_and_omissions(personal_client: AuthedClient, tmp_path: Path):
    p, _rid, _repo = _setup(personal_client, tmp_path)
    pack = _build(personal_client, p["id"], query=["compute_total"], paths=["big/*", "src/*.py"], token_budget=1500,
                  include=["repo_file", "code_match"])
    assert pack["token_estimate"] <= 1500
    assert pack["omitted"] and all(o["reason"] == "token budget" for o in pack["omitted"])
    assert any(i.get("truncated") for i in pack["items"]) or pack["omitted"]
    r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"token_budget": 10})
    assert r.status_code == 422


@needs_git
def test_pack_stale_when_base_changes(personal_client: AuthedClient, tmp_path: Path, secrets_memory):
    p, rid, repo_dir = _setup(personal_client, tmp_path)
    pack = _build(personal_client, p["id"], query=["compute_total"])
    assert personal_client.get(f"/api/context-packs/{pack['id']}").json()["context_pack"]["freshness"] == "CURRENT"
    commit_file(repo_dir, "src/billing.py", "def compute_total(items):\n    return 0\n")
    got = personal_client.get(f"/api/context-packs/{pack['id']}").json()["context_pack"]
    assert got["freshness"] == "STALE" and got["stale"] is True and "head moved" in got["stale_reason"]
    # a pack built after the change is current; the repo verify path also marks older packs stale
    pack2 = _build(personal_client, p["id"], query=["compute_total"])
    commit_file(repo_dir, "README.md", "x")
    with respx.mock() as mock:
        mock.get("https://api.github.com/repos/octo/ctx").respond(404)
        personal_client.post(f"/api/repositories/{rid}/verify")
    listed = personal_client.get(f"/api/projects/{p['id']}/context-packs").json()["items"]
    assert {x["id"]: x["stale"] for x in listed}[pack2["id"]] is True
    assert "content" not in listed[0]["items"][0]  # list view omits content


@needs_git
def test_pinned_commit_and_invalid_inputs(personal_client: AuthedClient, tmp_path: Path):
    p, _rid, repo_dir = _setup(personal_client, tmp_path)
    first = git(repo_dir, "rev-parse", "HEAD")
    commit_file(repo_dir, "src/new.py", "def compute_total_v2():\n    pass\n")
    pack = _build(personal_client, p["id"], base_commit=first, query=["compute_total_v2"], include=["symbol"])
    assert pack["base_commit"] == first and not [i for i in pack["items"] if i["kind"] == "symbol"]
    for body in ({"paths": ["../outside/*"]}, {"paths": ["/etc/*"]}, {"forbidden_paths": ["C:/x"]},
                 {"base_commit": "--output=/tmp/x"}, {"query": ["x" * 300]}, {"include": ["shell"]},
                 {"role_key": "Bad Role"}):
        r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json=body)
        assert r.status_code == 422, body


def test_pack_without_repository_reports_gap(personal_client: AuthedClient, db):
    p = create_project(personal_client, name="No Repo")
    db.add(ResearchEntry(name="Serena code retrieval", category="retrieval", relevance="symbol retrieval for agents",
                         urls=["https://github.com/oraios/serena"], evidence_level="E0", lifecycle="DISCOVERED"))
    db.commit()
    pack = _build(personal_client, p["id"], role_key="researcher", query=["retrieval"])
    assert not any("repository" in w for w in pack["warnings"])  # researcher profile has no repo context
    gap = _build(personal_client, p["id"], query=["retrieval"], include=["research", "symbol"])
    assert any("repository context unavailable" in w for w in gap["warnings"])
    research = [i for i in pack["items"] if i["kind"] == "research"]
    assert research and research[0]["trust"] == "DATA"
    assert research[0]["provenance"]["urls"] == ["https://github.com/oraios/serena"]


def test_attachment_context_is_untrusted(personal_client: AuthedClient):
    p = create_project(personal_client, name="Att Ctx")
    personal_client.post(f"/api/projects/{p['id']}/attachments",
                         files={"file": ("brief.md", b"# Brief\nThe invoice module must export CSV.\n", "text/markdown")})
    pack = _build(personal_client, p["id"], role_key="planner", query=["invoice"])
    att = [i for i in pack["items"] if i["kind"] == "attachment"]
    assert att and att[0]["trust"] == "UNTRUSTED_DATA" and "export CSV" in att[0]["content"]


def test_cross_domain_pack_is_404(admin_client: AuthedClient):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office Ctx")
    pack = _build(admin_client, p["id"])
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get(f"/api/context-packs/{pack['id']}").status_code == 404
    assert admin_client.get(f"/api/projects/{p['id']}/context-packs").status_code == 404


def test_policy_change_marks_pack_stale(personal_client: AuthedClient, monkeypatch):
    from aeos.knowledge import packs

    p = create_project(personal_client, name="Policy Ctx")
    pack = _build(personal_client, p["id"])
    assert any(i["kind"] == "policy" for i in pack["items"])
    real = packs.policy_hashes()
    monkeypatch.setattr(packs, "policy_hashes", lambda: {k: "sha256:changed" for k in real})
    got = personal_client.get(f"/api/context-packs/{pack['id']}").json()["context_pack"]
    assert got["freshness"] == "STALE" and "policy file" in got["stale_reason"]


@needs_git
def test_task_scoped_pack(personal_client: AuthedClient, tmp_path: Path, db):
    p, _rid, repo_dir = _setup(personal_client, tmp_path)
    head = git(repo_dir, "rev-parse", "HEAD")
    plan = Plan(project_id=p["id"], domain="PERSONAL", title="Billing")
    db.add(plan)
    db.flush()
    content = {"tasks": []}
    pv = PlanVersion(plan_id=plan.id, project_id=p["id"], domain="PERSONAL", version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    task = TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=p["id"], domain="PERSONAL",
                    task_key="T1", title="Fix totals", goal="Make compute_total handle discounts",
                    acceptance_criteria=["tests pass"], allowed_paths=["src/*.py"], forbidden_paths=["src/leaky.py"],
                    base_commit=head, data_classification="CONFIDENTIAL", spec_hash="sha256:x")
    db.add(task)
    db.commit()
    pack = _build(personal_client, p["id"], task_id=task.id, role_key="feature-implementer", query=["compute_total"])
    kinds = {i["kind"] for i in pack["items"]}
    assert "task" in kinds and pack["task_id"] == task.id and pack["role_key"] == "feature-implementer"
    assert pack["classification"] == "CONFIDENTIAL"
    paths = {i["provenance"].get("path") for i in pack["items"] if i["kind"] in {"symbol", "code_match"}}
    assert paths and all(x.startswith("src/") and x.endswith(".py") for x in paths)
    assert "src/leaky.py" not in paths and "src/api.ts" not in paths
    assert "src/leaky.py" in pack["forbidden_paths"]
    other = create_project(personal_client, name="Other")
    r = personal_client.post(f"/api/projects/{other['id']}/context-packs", json={"task_id": task.id})
    assert r.status_code == 404


def test_viewer_cannot_build_pack(client: AuthedClient, db):
    from aeos.models.portfolio import Project
    from tests.conftest import VIEWER_PW, create_admin, create_viewer

    create_admin(db)
    create_viewer(db)
    proj = Project(project_key="vp", name="VP", domain="PERSONAL")
    db.add(proj)
    db.commit()
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.post(f"/api/projects/{proj.id}/context-packs", json={}).status_code == 403
    assert client.get(f"/api/projects/{proj.id}/context-packs").status_code == 200


@pytest.mark.parametrize("term", ["a", ""])
def test_short_terms_ignored_or_rejected(personal_client: AuthedClient, term: str):
    p = create_project(personal_client, name=f"T{term}")
    r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"query": [term]})
    assert r.status_code == 422
