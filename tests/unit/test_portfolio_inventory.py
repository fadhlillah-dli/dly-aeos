"""Existing-project inventory (§05 step 6, §37 coverage): read-only, exact commit, no scaffolding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeos.core.errors import ValidationFailed
from aeos.portfolio.inventory import inventory_repository
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import commit_file, create_project, git, make_repo, needs_git

FILES = {
    "README.md": "# Demo\n",
    "docs/guide.md": "guide\n",
    "docs/decisions/ADR-0001-x.md": "adr\n",
    "LICENSE": "MIT\n",
    "backend/app.py": "def main():\n    return 1\n",
    "backend/util.py": "x = 1\n",
    "frontend/src/main.ts": "export const a = 1\n",
    "frontend/src/App.tsx": "export default function App() { return null }\n",
    "frontend/src/App.test.tsx": "test('x', () => {})\n",
    "frontend/package.json": json.dumps({"devDependencies": {"vitest": "^5", "typescript": "^7"}}),
    "pyproject.toml": "[project]\nname='demo'\n[tool.pytest.ini_options]\naddopts='-q'\n",
    "requirements.txt": "fastapi\n",
    "go.mod": "module demo\n",
    "go.sum": "",
    "tests/test_app.py": "def test_x():\n    assert True\n",
    ".github/workflows/ci.yml": "on: push\n",
    "Dockerfile": "FROM python:3.12\n",
    "infra/main.tf": "resource \"x\" \"y\" {}\n",
    "api/openapi.yaml": "openapi: 3.1.0\n",
    "proto/svc.proto": "syntax = \"proto3\";\n",
    "backend/migrations/0001_init.sql": "create table t(id int);\n",
    ".env": "SECRET=1\n",
}


@needs_git
def test_inventory_facts(tmp_path: Path):
    repo = make_repo(tmp_path / "demo", FILES, symlinks={"link-out": "../../outside"})
    head = git(repo, "rev-parse", "HEAD")
    before = git(repo, "status", "--porcelain")
    inv = inventory_repository(repo)
    assert inv["commit"] == head and inv["branch"] == "main"
    assert inv["working_tree"]["dirty"] is False
    langs = {x["language"]: x["files"] for x in inv["languages"]}
    assert langs["Python"] >= 3 and langs["TypeScript"] == 3
    manifests = {m["path"]: m for m in inv["dependency_manifests"]}
    assert manifests["frontend/package.json"]["lockfile_status"] == "MISSING"
    assert manifests["go.mod"]["lockfile_status"] == "PRESENT" and manifests["go.mod"]["lockfiles"] == ["go.sum"]
    assert "requirements.txt" in manifests
    assert {"vitest", "pytest"} <= set(inv["tests"]["frameworks"])
    assert "tests" in inv["tests"]["directories"]
    assert inv["tests"]["baseline"]["status"] == "NOT_RUN"
    assert inv["ci"] == {"GitHub Actions": [".github/workflows/ci.yml"]}
    assert "container" in inv["deployment"] and "terraform" in inv["deployment"]
    assert {"openapi", "protobuf", "db-migrations"} <= set(inv["contracts"])
    assert {"readme", "docs", "adr", "license"} <= set(inv["docs"])
    assert inv["symlinks"] == ["link-out"]  # recorded, never followed
    assert ".env" in inv["sensitive_paths"]
    assert inv["scaffolded"] is False and inv["coverage_gaps"]
    assert inv["inventory_hash"].startswith("sha256:")
    assert git(repo, "status", "--porcelain") == before  # read-only: nothing created or changed


@needs_git
def test_inventory_at_exact_commit(tmp_path: Path):
    repo = make_repo(tmp_path / "hist", {"a.py": "x=1\n"})
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "b.ts", "export const b = 1\n")
    old = inventory_repository(repo, commit=first)
    new = inventory_repository(repo)
    assert old["commit"] == first and old["files"] == 1
    assert new["files"] == 2 and new["commit"] != first
    with pytest.raises(ValidationFailed):
        inventory_repository(repo, commit="--upload-pack=evil")
    with pytest.raises(ValidationFailed):
        inventory_repository(repo, commit="0" * 40)


@needs_git
def test_inventory_rejects_non_repo(tmp_path: Path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(ValidationFailed):
        inventory_repository(tmp_path / "plain")


@needs_git
def test_inventory_api_stores_latest(personal_client: AuthedClient, tmp_path: Path):
    repo_dir = make_repo(tmp_path / "apirepo", {"main.py": "print(1)\n"}, origin="https://github.com/octo/apirepo.git")
    p = create_project(personal_client)
    rid = personal_client.post(f"/api/projects/{p['id']}/repositories", json={
        "provider": "GITHUB", "owner": "octo", "name": "apirepo", "local_path": str(repo_dir)}).json()["repository"]["id"]
    r = personal_client.post(f"/api/repositories/{rid}/inventory", json={})
    assert r.status_code == 200, r.text
    assert r.json()["inventory"]["files"] == 1
    got = personal_client.get(f"/api/repositories/{rid}/inventory").json()
    assert got["commit"] == git(repo_dir, "rev-parse", "HEAD") and got["inventory"]["languages"][0]["language"] == "Python"
    detail = personal_client.get(f"/api/projects/{p['id']}").json()["project"]
    assert detail["repositories"][0]["inventory"]["commit"] == got["commit"]


def test_inventory_without_checkout_blocked(personal_client: AuthedClient):
    p = create_project(personal_client)
    rid = personal_client.post(f"/api/projects/{p['id']}/repositories", json={
        "provider": "GITHUB", "owner": "octo", "name": "remote-only"}).json()["repository"]["id"]
    r = personal_client.post(f"/api/repositories/{rid}/inventory", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "LOCAL_CHECKOUT_MISSING"


@needs_git
def test_platform_repo_inventory_for_planning():
    """The contract planning calls (``aeos.portfolio.inventory.inventory_repository``) on the self project."""
    from aeos.core.paths import repo_root

    try:
        inv = inventory_repository(repo_root())
    except ValidationFailed:
        pytest.skip("not running from a git checkout")
    assert inv["files"] > 10 and any(x["language"] == "Python" for x in inv["languages"])
