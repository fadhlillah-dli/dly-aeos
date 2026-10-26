"""§09 zones (W6-030): the UI bundle's commit correspondence recorded in the release provenance is what the build
CLI prints and what 'aeos ops release list', GET /api/ops/releases and the Control Center show - never a fixed
'UNVERIFIED' label, and an UNVERIFIED bundle shows its reason."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeos.operations import cli as ops_cli
from aeos.operations import release

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def bundles(tmp_path: Path) -> dict[str, dict]:
    """Real ui provenance blocks from release._build_ui_bundle: one VERIFIED, one without a build record."""
    r = tmp_path / "repo"
    (r / "frontend" / "src").mkdir(parents=True)
    (r / "frontend" / "src" / "main.ts").write_text("export const v = 1;\n", encoding="utf-8")
    (r / ".gitignore").write_text("frontend/dist/\n", encoding="utf-8")
    _git(r.parent, "init", "-q", str(r))
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    dist = r / "frontend" / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    commit = _git(r, "rev-parse", "HEAD")
    out = tmp_path / "out"
    out.mkdir()
    unverified = release._build_ui_bundle(r, out, "r-unverified", commit)
    (dist / "aeos-build.json").write_text(json.dumps({"schema": 1, "frontend_tree": _git(r, "rev-parse", "HEAD:frontend"),
                                                      "clean": True, "head": commit}), encoding="utf-8")
    verified = release._build_ui_bundle(r, out, "r-verified", commit)
    assert unverified and verified
    return {"VERIFIED": verified, "UNVERIFIED": unverified}


def test_build_cli_prints_the_recorded_correspondence(settings, bundles, monkeypatch):
    for corr, ui in bundles.items():
        prov = {"release_id": f"1.0.0-{corr.lower()}", "commit": "c" * 40, "tree_hash": "t" * 40, "artifact": "a.zip",
                "artifact_sha256": "s" * 64, "schema_head": None, "acceptance": {"status": "ACCEPTED", "basis": "x"},
                "ui": ui}
        monkeypatch.setattr(release, "build_release", lambda *a, _p=prov, **k: _p)
        r = CliRunner().invoke(ops_cli.app, ["release", "build"])
        assert r.exit_code == 0, r.output
        assert f"(commit correspondence {corr}" in r.output and r.output.isascii()
        if corr == "UNVERIFIED":
            assert "no build record of its source tree" in r.output
        else:
            assert "UNVERIFIED" not in r.output
    assert ops_cli._ui_line(None) == "none (frontend not built)"


def _write_provenance(settings, release_id: str, ui: dict | None) -> None:
    out = settings.layout.released_controller
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{release_id}.zip").write_bytes(b"zip")
    prov = {"format": release.FORMAT, "release_id": release_id, "version": "1.0.0", "commit": "c" * 40,
            "tree_hash": "t" * 40, "artifact": f"{release_id}.zip", "artifact_sha256": "s" * 64,
            "built_at": f"2026-10-2{len(release_id) % 9}T00:00:00+00:00", "ui": ui}
    (out / f"{release_id}.provenance.json").write_text(json.dumps(prov), encoding="utf-8")


def test_release_list_shows_the_ui_correspondence(settings, bundles, personal_client):
    _write_provenance(settings, "1.0.0-verified", bundles["VERIFIED"])
    _write_provenance(settings, "1.0.0-unverified", bundles["UNVERIFIED"])
    _write_provenance(settings, "1.0.0-noui", None)
    items = {i["release_id"]: i for i in release.list_releases(settings)["items"]}
    assert items["1.0.0-verified"]["ui"]["commit_correspondence"] == "VERIFIED"
    assert items["1.0.0-verified"]["ui"]["sha256"] == bundles["VERIFIED"]["sha256"]
    unv = items["1.0.0-unverified"]["ui"]
    assert unv["commit_correspondence"] == "UNVERIFIED" and "no build record" in unv["reason"]
    assert items["1.0.0-noui"]["ui"] is None

    r = CliRunner().invoke(ops_cli.app, ["release", "list"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert any("1.0.0-verified" in ln and "ui=VERIFIED" in ln for ln in lines)
    assert any("1.0.0-unverified" in ln and "ui=UNVERIFIED" in ln for ln in lines)
    assert any("1.0.0-noui" in ln and "ui=NONE" in ln for ln in lines)
    assert any(ln.strip().startswith("ui: the bundle has no build record") for ln in lines)

    api = {i["release_id"]: i for i in personal_client.get("/api/ops/releases").json()["items"]}
    assert api["1.0.0-unverified"]["ui"]["commit_correspondence"] == "UNVERIFIED"


def _main_repo(tmp_path: Path) -> Path:
    r = tmp_path / "main-repo"
    (r / "backend" / "aeos").mkdir(parents=True)
    (r / "backend" / "aeos" / "__init__.py").write_text('__version__ = "9.9.9"\nSCHEMA_VERSION = "1"\n', encoding="utf-8")
    (r / "frontend" / "src").mkdir(parents=True)
    (r / "frontend" / "src" / "main.ts").write_text("export const v = 1;\n", encoding="utf-8")
    (r / ".gitignore").write_text("frontend/dist/\n", encoding="utf-8")
    _git(r.parent, "init", "-q", "-b", "main", str(r))
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "release base")
    (r / "frontend" / "dist").mkdir()
    (r / "frontend" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return r


def test_build_refuses_an_unverified_bundle_unless_the_owner_allows_it(settings, personal_client, db, tmp_path):
    """W6-030 fail closed: a Control Center bundle not proven to come from the release commit (no build record /
    dirty tree) refuses the build before anything is written; the owner's explicit --allow-unverified-ui pins it
    with that choice recorded in the provenance and the audit. A VERIFIED bundle builds without the flag."""
    from sqlalchemy import select

    from aeos.core.errors import Blocked
    from aeos.models.audit import AuditEvent
    from tests.acceptance.world import owner_id

    repo = _main_repo(tmp_path)
    commit = _git(repo, "rev-parse", "HEAD")
    release.record_acceptance_exception(db, commit=commit, operator_user_id=owner_id(db),
                                        reason="unit test: no provider CI for this local repository")
    db.commit()
    with pytest.raises(Blocked) as exc:
        release.build_release(settings, repo=repo, session=db)
    assert exc.value.code == "RELEASE_UI_UNVERIFIED" and "no build record" in str(exc.value)
    assert not list(settings.layout.released_controller.glob("*.zip"))  # refused before anything was written

    r = CliRunner().invoke(ops_cli.app, ["release", "build", "--repo", str(repo)])
    assert r.exit_code == 1 and "RELEASE_UI_UNVERIFIED" in r.output and "--allow-unverified-ui" in r.output

    prov = release.build_release(settings, repo=repo, session=db, builder="owner-cli", allow_unverified_ui=True)
    db.commit()
    assert prov["ui"]["commit_correspondence"] == "UNVERIFIED" and prov["ui"]["unverified_allowed_by"] == "owner-cli"
    assert "pinned by owner choice (--allow-unverified-ui, owner-cli)" in ops_cli._ui_line(prov["ui"])
    listed = next(i for i in release.list_releases(settings)["items"] if i["release_id"] == prov["release_id"])
    assert listed["ui"]["unverified_allowed_by"] == "owner-cli"
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.release.build"))
    assert audit.details["ui_commit_correspondence"] == "UNVERIFIED" and audit.details["ui_unverified_allowed"] is True

    (repo / "frontend" / "dist" / "aeos-build.json").write_text(json.dumps(
        {"schema": 1, "frontend_tree": _git(repo, "rev-parse", "HEAD:frontend"), "clean": True, "head": commit}),
        encoding="utf-8")
    assert release.ui_correspondence(repo, commit, repo / "frontend" / "dist")[0] == "VERIFIED"


def test_build_with_a_verified_bundle_needs_no_flag(settings, personal_client, db, tmp_path):
    from tests.acceptance.world import owner_id

    repo = _main_repo(tmp_path)
    commit = _git(repo, "rev-parse", "HEAD")
    (repo / "frontend" / "dist" / "aeos-build.json").write_text(json.dumps(
        {"schema": 1, "frontend_tree": _git(repo, "rev-parse", "HEAD:frontend"), "clean": True, "head": commit}),
        encoding="utf-8")
    release.record_acceptance_exception(db, commit=commit, operator_user_id=owner_id(db),
                                        reason="unit test: no provider CI for this local repository")
    db.commit()
    prov = release.build_release(settings, repo=repo, session=db)
    assert prov["ui"]["commit_correspondence"] == "VERIFIED" and "unverified_allowed_by" not in prov["ui"]


def test_old_provenance_without_a_correspondence_is_unverified_not_assumed(settings):
    _write_provenance(settings, "0.9.0-legacy", {"artifact": "x.ui.zip", "sha256": "u" * 64})
    item = release.list_releases(settings)["items"][0]
    assert item["ui"]["commit_correspondence"] == "UNVERIFIED"
