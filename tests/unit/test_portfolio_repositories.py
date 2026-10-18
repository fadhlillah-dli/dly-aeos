"""Repository references (§04, §37, §50 path traversal/symlink, identity never guessed)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.models.audit import AuditEvent
from aeos.models.portfolio import RepositoryReference
from aeos.portfolio.repositories import remote_matches
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import commit_file, create_project, make_repo, needs_git

GH = "https://api.github.com/repos"


def _add(client: AuthedClient, pid: str, **body) -> httpx.Response:
    body.setdefault("provider", "GITHUB")
    body.setdefault("owner", "octo")
    body.setdefault("name", "demo")
    return client.post(f"/api/projects/{pid}/repositories", json=body)


def test_personal_requires_github(personal_client: AuthedClient, db):
    p = create_project(personal_client)
    r = _add(personal_client, p["id"], provider="BITBUCKET")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PROVIDER_DOMAIN_MISMATCH"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "portfolio.repository.add",
                                              AuditEvent.result == "DENIED"))
    r = _add(personal_client, p["id"])
    assert r.status_code == 200, r.text
    repo = r.json()["repository"]
    assert repo["provider"] == "GITHUB" and repo["full_name"] == "octo/demo"
    assert repo["verification_status"] == "UNVERIFIED" and repo["url_source"] == "declared"
    assert _add(personal_client, p["id"]).status_code == 409  # duplicate


def test_office_requires_bitbucket(admin_client: AuthedClient, secrets_memory):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office Repo")
    r = _add(admin_client, p["id"], provider="GITHUB")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PROVIDER_DOMAIN_MISMATCH"
    r = _add(admin_client, p["id"], provider="BITBUCKET", owner="company", name="svc")
    assert r.status_code == 200
    rid = r.json()["repository"]["id"]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        v = admin_client.post(f"/api/repositories/{rid}/verify")
        assert not mock.calls  # no Office provider call without company authorisation
    assert v.status_code == 200
    assert v.json()["verification"]["verification_status"] == "UNVERIFIED"
    assert "Bitbucket" in v.json()["verification"]["reason"]


@pytest.mark.parametrize("owner,name", [("-bad", "x"), ("ok", "../etc"), ("ok", ".."), ("a b", "x"), ("ok", "x/y")])
def test_invalid_identity_rejected(personal_client: AuthedClient, owner: str, name: str):
    p = create_project(personal_client)
    assert _add(personal_client, p["id"], owner=owner, name=name).status_code == 422


def test_platform_role_only_for_platform_project(personal_client: AuthedClient):
    p = create_project(personal_client)
    r = _add(personal_client, p["id"], role="platform")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PLATFORM_ROLE_DENIED"


def test_github_verify_success_uses_token_without_leaking(personal_client: AuthedClient, db, secrets_memory):
    token = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    secrets_memory.put("personal/github", token, actor="test")
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    payload = {"id": 4242, "full_name": "octo/demo", "default_branch": "develop", "private": True,
               "visibility": "private", "fork": False, "html_url": "https://github.com/octo/demo",
               "owner": {"type": "User"}, "permissions": {"admin": True, "push": True, "pull": True}}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{GH}/octo/demo").respond(200, json=payload)
        r = personal_client.post(f"/api/repositories/{rid}/verify")
    assert r.status_code == 200, r.text
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {token}"
    assert route.calls.last.request.method == "GET"
    repo = r.json()["repository"]
    assert repo["verification_status"] == "VERIFIED"
    assert repo["provider_repo_id"] == "4242" and repo["default_branch"] == "develop"
    assert repo["last_verified_at"] and repo["url_source"] == "provider"
    assert repo["github"]["private"] is True
    assert token not in r.text
    db.expire_all()
    audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "portfolio.repository.verify")))
    assert audits and all(token not in str(a.details) for a in audits)


def test_github_404_stays_unverified(personal_client: AuthedClient, secrets_memory):
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(404, json={"message": "Not Found"})
        r = personal_client.post(f"/api/repositories/{rid}/verify")
    repo = r.json()["repository"]
    assert repo["verification_status"] == "UNVERIFIED"
    assert "not found" in repo["verification_reason"] and "personal/github" in repo["verification_reason"]
    assert repo["provider_repo_id"] is None and repo["last_verified_at"] is None


@pytest.mark.parametrize("status,needle", [(401, "401"), (403, "rate-limited"), (500, "unexpected"),
                                           (301, "moved/renamed")])
def test_github_failures_never_guess(personal_client: AuthedClient, secrets_memory, status: int, needle: str):
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(status, headers={"Location": f"{GH}/other/demo"} if status == 301 else {})
        repo = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert repo["verification_status"] == "UNVERIFIED" and needle in repo["verification_reason"]
    assert repo["full_name"] == "octo/demo"


def test_github_rename_or_id_change_is_unverified(personal_client: AuthedClient, secrets_memory):
    """§37/§50: a repository with the same name is not the same target; renames are not trusted."""
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(200, json={"id": 1, "full_name": "someone-else/demo"})
        repo = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert repo["verification_status"] == "UNVERIFIED" and "mismatch" in repo["verification_reason"]
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(200, json={"id": 1, "full_name": "octo/demo"})
        assert personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]["verification_status"] == "VERIFIED"
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(200, json={"id": 2, "full_name": "octo/demo"})
        repo = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert repo["verification_status"] == "UNVERIFIED" and "id changed" in repo["verification_reason"]
    assert repo["provider_repo_id"] == "1"


def test_github_network_error(personal_client: AuthedClient, secrets_memory):
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").mock(side_effect=httpx.ConnectTimeout("timeout"))
        repo = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert repo["verification_status"] == "UNVERIFIED" and "network error" in repo["verification_reason"]


def test_verify_without_secret_store_is_anonymous(personal_client: AuthedClient, monkeypatch):
    from aeos.core.errors import Blocked
    from aeos.secrets import facade

    def _blocked(*_a, **_k):
        raise Blocked("no store", code="SECRET_STORE_UNAVAILABLE")

    monkeypatch.setattr(facade, "get_secret", _blocked)
    p = create_project(personal_client)
    rid = _add(personal_client, p["id"]).json()["repository"]["id"]
    with respx.mock() as mock:
        route = mock.get(f"{GH}/octo/demo").respond(200, json={"id": 7, "full_name": "octo/demo"})
        repo = personal_client.post(f"/api/repositories/{rid}/verify").json()["repository"]
    assert "Authorization" not in route.calls.last.request.headers
    assert repo["verification_status"] == "VERIFIED" and "anonymous" in repo["verification_reason"]


def test_cross_domain_repository_404(admin_client: AuthedClient):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="O")
    rid = _add(admin_client, p["id"], provider="BITBUCKET", owner="co", name="r").json()["repository"]["id"]
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.post(f"/api/repositories/{rid}/verify").status_code == 404
    assert admin_client.get(f"/api/repositories/{rid}").status_code == 404


def test_remote_matching_is_exact():
    assert remote_matches("https://github.com/octo/demo.git", "GITHUB", "octo/demo")
    assert remote_matches("git@github.com:Octo/Demo.git", "GITHUB", "octo/demo")
    assert remote_matches("ssh://git@github.com/octo/demo", "GITHUB", "octo/demo")
    assert not remote_matches("https://github.com/octo/demo-fork.git", "GITHUB", "octo/demo")
    assert not remote_matches("https://evil.example/octo/demo.git", "GITHUB", "octo/demo")
    assert not remote_matches("https://bitbucket.org/octo/demo.git", "GITHUB", "octo/demo")
    assert not remote_matches(None, "GITHUB", "octo/demo")


@needs_git
def test_local_checkout_observed_read_only(personal_client: AuthedClient, tmp_path: Path, secrets_memory):
    repo_dir = make_repo(tmp_path / "work" / "demo", {"README.md": "# demo\n", "app.py": "print(1)\n"},
                         origin="https://ghp_secretinurl1234567890@github.com/octo/demo.git")
    p = create_project(personal_client)
    r = _add(personal_client, p["id"], local_path=str(repo_dir))
    assert r.status_code == 200, r.text
    repo = r.json()["repository"]
    assert repo["local_status"] == "OK" and repo["local_branch"] == "main"
    assert repo["dirty"] is False and len(repo["head_commit"]) == 40
    assert str(repo_dir) not in r.text  # local paths are shown redacted/logical (§37)
    assert repo["local_path"].endswith("work/demo") or repo["local_path"].endswith("demo")
    assert "ghp_secretinurl" not in r.text  # credentials in remote URLs are stripped
    (repo_dir / "new.txt").write_text("x")
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(404)
        v = personal_client.post(f"/api/repositories/{repo['id']}/verify").json()["repository"]
    assert v["dirty"] is True
    assert (repo_dir / "new.txt").exists()  # nothing was cleaned, checked out or reset


@needs_git
def test_local_checkout_origin_mismatch(personal_client: AuthedClient, tmp_path: Path):
    repo_dir = make_repo(tmp_path / "other", {"a.txt": "a"}, origin="https://github.com/octo/another.git")
    p = create_project(personal_client)
    repo = _add(personal_client, p["id"], local_path=str(repo_dir)).json()["repository"]
    assert repo["local_status"] == "MISMATCH"
    assert "does not point" in repo["local"]["reason"]


@needs_git
def test_local_path_validation(personal_client: AuthedClient, tmp_path: Path, settings):
    """§50 "File path traversal/symlink keluar sandbox": paths are resolved and validated."""
    repo_dir = make_repo(tmp_path / "valid", {"src/a.py": "x = 1\n"})
    plain = tmp_path / "plain"
    plain.mkdir()
    p = create_project(personal_client)
    cases = {
        "relative/path": "INVALID_LOCAL_PATH",
        str(tmp_path / "missing"): "LOCAL_PATH_NOT_FOUND",
        str(plain): "NOT_A_GIT_REPO",
        str(repo_dir / "src"): "NOT_REPO_TOPLEVEL",
        str(repo_dir / "src" / ".." / ".." / "plain"): "NOT_A_GIT_REPO",
        "\\\\server\\share\\repo": "INVALID_LOCAL_PATH",
        "//server/share/repo": "INVALID_LOCAL_PATH",
    }
    for i, (path, code) in enumerate(cases.items()):
        r = _add(personal_client, p["id"], name=f"r{i}", local_path=path)
        assert r.status_code in (403, 422), (path, r.text)
        assert r.json()["error"]["code"] == code, (path, r.json())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    make_repo(settings.data_dir / "inner", {"x": "y"})
    r = _add(personal_client, p["id"], name="inside-data", local_path=str(settings.data_dir / "inner"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "LOCAL_PATH_IN_DATA_DIR"


@needs_git
def test_head_change_detected(personal_client: AuthedClient, tmp_path: Path, secrets_memory, db):
    repo_dir = make_repo(tmp_path / "moving", {"a.txt": "1"}, origin="https://github.com/octo/demo.git")
    p = create_project(personal_client)
    repo = _add(personal_client, p["id"], local_path=str(repo_dir)).json()["repository"]
    new_head = commit_file(repo_dir, "a.txt", "2")
    with respx.mock() as mock:
        mock.get(f"{GH}/octo/demo").respond(404)
        v = personal_client.post(f"/api/repositories/{repo['id']}/verify").json()["repository"]
    assert v["head_commit"] == new_head != repo["head_commit"]
    db.expire_all()
    assert db.get(RepositoryReference, repo["id"]).head_commit == new_head
