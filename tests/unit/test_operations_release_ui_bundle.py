"""§09 zones: the released Control Center bundle (the approval UI) is tied to the release commit - VERIFIED only
when the Vite build's record of its source tree equals ``git rev-parse <commit>:frontend``; a bundle built from
another tree refuses the release; without a record it stays UNVERIFIED (never assumed)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aeos.core.errors import Conflict
from aeos.operations import release

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "frontend" / "src").mkdir(parents=True)
    (r / "frontend" / "src" / "main.ts").write_text("export const v = 1;\n", encoding="utf-8")
    (r / ".gitignore").write_text("frontend/dist/\n", encoding="utf-8")
    _git(r.parent, "init", "-q", str(r))
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (r / "frontend" / "dist").mkdir()
    (r / "frontend" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return r


def _record(repo: Path, tree: str | None, clean: bool = True) -> None:
    (repo / "frontend" / "dist" / "aeos-build.json").write_text(
        json.dumps({"schema": 1, "frontend_tree": tree, "clean": clean, "head": None}), encoding="utf-8")


def test_bundle_built_from_the_release_commit_is_verified(repo, tmp_path):
    commit = _git(repo, "rev-parse", "HEAD")
    _record(repo, _git(repo, "rev-parse", "HEAD:frontend"))
    status, detail = release.ui_correspondence(repo, commit, repo / "frontend" / "dist")
    assert status == "VERIFIED" and detail["commit_frontend_tree"] == detail["bundle_frontend_tree"]
    out = tmp_path / "out"
    out.mkdir()
    ui = release._build_ui_bundle(repo, out, "r1", commit)
    assert ui is not None and ui["commit_correspondence"] == "VERIFIED" and ui["file_count"] == 2


def test_bundle_from_another_tree_refuses_the_release(repo, tmp_path):
    commit = _git(repo, "rev-parse", "HEAD")
    _record(repo, "f" * 40)
    assert release.ui_correspondence(repo, commit, repo / "frontend" / "dist")[0] == "MISMATCH"
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(Conflict) as exc:
        release._build_ui_bundle(repo, out, "r1", commit)
    assert exc.value.code == "RELEASE_UI_MISMATCH"


def test_without_a_clean_build_record_it_stays_unverified(repo):
    commit = _git(repo, "rev-parse", "HEAD")
    dist = repo / "frontend" / "dist"
    assert release.ui_correspondence(repo, commit, dist)[0] == "UNVERIFIED"
    _record(repo, _git(repo, "rev-parse", "HEAD:frontend"), clean=False)
    status, detail = release.ui_correspondence(repo, commit, dist)
    assert status == "UNVERIFIED" and "uncommitted" in detail["reason"]


def test_the_vite_build_writes_the_source_tree_record():
    from aeos.core.paths import repo_root

    cfg = (repo_root() / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    assert "aeos-build.json" in cfg and '"rev-parse", "HEAD:./"' in cfg and "aeosBuildInfo()" in cfg
