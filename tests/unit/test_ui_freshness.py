"""R28 / LESSONS L23: what ``aeos serve`` serves stays current after a ``git pull``.

* Doctor ``runtime.frontend_dist`` compares the build record with ``HEAD:frontend`` (FRONTEND_STALE when it
  differs, UNVERIFIED without a clean record, VERIFIED after a backend-only commit).
* ``aeos serve`` rebuilds a stale bundle once when node_modules matches the lockfile and frontend/ is clean, never
  installs anything and never blocks the start; /api/health reports ``ui_build``.
* Cache headers: index.html and the SPA fallback revalidate, hashed assets are immutable, a missing asset is 404.
* Doctor ``runtime.code_drift``: the running server's loaded commit against HEAD (backend paths only).

Tests never run a real npm build (tests/conftest.py refuses one; these tests inject a fake runner).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import compute_fingerprint, make_context, run_check
from aeos.operations import freshness, release
from tests.conftest import TEST_GIT_EMAIL, TEST_GIT_NAME, make_app
from tests.unit.test_inventory_fakes import base_runner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ROOT = Path(__file__).resolve().parents[2]
LOCK = {"name": "x", "lockfileVersion": 3, "requires": True, "packages": {
    "": {"name": "x", "version": "0.1.0"},
    "node_modules/a": {"version": "1.0.0", "integrity": "sha512-a"},
    "node_modules/@b/linux-only": {"version": "2.0.0", "integrity": "sha512-b", "optional": True},
}}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", f"user.name={TEST_GIT_NAME}", "-c",
                           f"user.email={TEST_GIT_EMAIL}", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"change {rel}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "frontend" / "src").mkdir(parents=True)
    (r / "frontend" / "src" / "main.ts").write_text("export const v = 1;\n", encoding="utf-8")
    (r / "frontend" / "package-lock.json").write_text(json.dumps(LOCK), encoding="utf-8")
    (r / "backend").mkdir()
    (r / "backend" / "app.py").write_text("V = 1\n", encoding="utf-8")
    (r / "docs").mkdir()
    (r / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    (r / ".gitignore").write_text("frontend/dist/\nfrontend/node_modules/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    dist = r / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AEOS</title>", encoding="utf-8")
    return r


def _stamp(repo: Path, tree: str | None, clean: bool = True) -> None:
    (repo / "frontend" / "dist" / "aeos-build.json").write_text(
        json.dumps({"schema": 1, "frontend_tree": tree, "clean": clean, "head": None}), encoding="utf-8")


def _head_tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD:frontend")


def _install(repo: Path, lock: dict[str, Any] | None = None) -> None:
    """node_modules as npm ci leaves it: the hidden lockfile lists the installed packages (no root entry; the
    optional platform package of another host is absent)."""
    lock = lock or LOCK
    nm = repo / "frontend" / "node_modules"
    nm.mkdir(parents=True, exist_ok=True)
    pkgs = {k: v for k, v in lock["packages"].items() if k and not v.get("optional")}
    (nm / ".package-lock.json").write_text(json.dumps({"name": "x", "packages": pkgs}), encoding="utf-8")


def _doctor(settings, db, cid: str, repo: Path):
    load_all()
    return run_check(REGISTRY[cid], make_context(settings, db, runner=base_runner(), repo=repo))


# ----------------------------------------------------------------------------------------- doctor: frontend_dist


def test_a_bundle_from_another_frontend_tree_is_frontend_stale(settings, db, repo):
    _stamp(repo, "f" * 40)
    res = _doctor(settings, db, "runtime.frontend_dist", repo)
    assert res.status == "CONFIGURED" and res.error_code == "FRONTEND_STALE"
    assert f"bundle built from frontend tree ffffffffffff, HEAD has {_head_tree(repo)[:12]}" in res.summary
    assert "npm" in res.remediation["command"] and "run build" in res.remediation["command"]
    assert "Restart aeos serve" in res.remediation["operation"]


def test_a_backend_only_commit_stays_verified_and_keeps_the_fingerprint(settings, db, repo):
    _stamp(repo, _head_tree(repo))
    load_all()
    spec = REGISTRY["runtime.frontend_dist"]
    ctx = make_context(settings, db, runner=base_runner(), repo=repo)
    assert run_check(spec, ctx).status == "VERIFIED"
    before = compute_fingerprint(spec, ctx)
    _commit(repo, "backend/app.py", "V = 2\n")
    assert run_check(spec, ctx).status == "VERIFIED"
    assert compute_fingerprint(spec, ctx) == before  # no re-run needed for a backend-only pull
    _commit(repo, "frontend/src/main.ts", "export const v = 2;\n")
    assert compute_fingerprint(spec, ctx) != before  # a frontend pull invalidates the previous result
    res = run_check(spec, ctx)
    assert res.status == "CONFIGURED" and res.error_code == "FRONTEND_STALE"


def test_no_build_record_or_a_dirty_build_is_unverified_never_verified(settings, db, repo):
    res = _doctor(settings, db, "runtime.frontend_dist", repo)
    assert res.status == "UNKNOWN" and res.error_code == "FRONTEND_BUILD_UNVERIFIED" and "UNVERIFIED" in res.summary
    _stamp(repo, _head_tree(repo), clean=False)
    res = _doctor(settings, db, "runtime.frontend_dist", repo)
    assert res.status == "UNKNOWN" and "uncommitted" in res.summary


def test_the_checkout_comparison_is_release_ui_correspondence(settings, repo, monkeypatch):
    """Single owner: the checkout path asks release.ui_correspondence with commit HEAD."""
    seen: list[str] = []

    def spy(r: Path, commit: str, dist: Path) -> tuple[str, dict[str, Any]]:
        seen.append(commit)
        return "VERIFIED", {"bundle_frontend_tree": "t"}

    monkeypatch.setattr(release, "ui_correspondence", spy)
    assert freshness.bundle_state(settings, repo)["status"] == "FRESH" and seen == ["HEAD"]


@pytest.mark.parametrize(("corr", "expected"), [("VERIFIED", "FRESH"), ("UNVERIFIED", "UNVERIFIED")])
def test_a_release_install_uses_the_correspondence_its_provenance_recorded(settings, monkeypatch, corr, expected):
    rid = "0.1.0-abcdef123456"
    rel = settings.layout.released_controller
    root = rel / rid
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (rel / f"{rid}.provenance.json").write_text(json.dumps({
        "format": release.FORMAT, "release_id": rid, "commit": "a" * 40,
        "ui": {"sha256": "x", "commit_correspondence": corr, "correspondence": {"bundle_frontend_tree": "t" * 40,
                                                                                "reason": "no record"}}}),
        encoding="utf-8")
    monkeypatch.setattr(release, "ui_correspondence",
                        lambda *a: pytest.fail("a release has no git: its build-time verdict is used"))
    state = freshness.bundle_state(settings, root)
    assert state["status"] == expected and state["basis"] == "release" and state["release_id"] == rid


def test_outside_a_git_checkout_the_bundle_is_unverified(settings, tmp_path):
    root = tmp_path / "plain"
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    state = freshness.bundle_state(settings, root)
    assert state["status"] == "UNVERIFIED" and "not a git checkout" in state["reason"]


# ----------------------------------------------------------------------------------------- serve self-heal


class FakeBuild:
    """Stands in for 'npm run build': records the call and writes the record a real build would write."""

    def __init__(self, repo: Path, code: int = 0, exc: BaseException | None = None) -> None:
        self.repo, self.code, self.exc, self.calls = repo, code, exc, []

    def __call__(self, argv: list[str], cwd: Path, timeout: float, log_path: Path) -> int:
        self.calls.append(argv)
        if self.exc is not None:
            raise self.exc
        if self.code == 0:
            _stamp(self.repo, _head_tree(self.repo))
        return self.code


def _ensure(settings, repo: Path, **kw: Any) -> tuple[dict[str, Any], list[str]]:
    lines: list[str] = []
    rec = freshness.ensure_ui_fresh(settings, repo, remedy="REMEDY", which=lambda n: f"/fake/bin/{n}",
                                    echo=lines.append, **kw)
    return rec, lines


def test_a_stale_bundle_is_rebuilt_once_with_the_injected_runner(settings, repo, monkeypatch):
    _stamp(repo, "f" * 40)
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert fake.calls == [["/fake/bin/npm", "run", "build"]]
    assert rec["status"] == "REBUILT" and len(lines) == 1 and "running 'npm run build' once" in lines[0]


@pytest.mark.parametrize("case", ["dirty", "no_node_modules", "lock_changed", "disabled"])
def test_a_stale_bundle_is_not_rebuilt_when_it_is_not_safe(settings, repo, monkeypatch, case):
    _stamp(repo, "f" * 40)
    _install(repo)
    if case == "dirty":
        (repo / "frontend" / "src" / "draft.ts").write_text("x\n", encoding="utf-8")
    elif case == "no_node_modules":
        shutil.rmtree(repo / "frontend" / "node_modules")
    elif case == "lock_changed":  # a pulled lockfile adds a dependency node_modules does not have yet
        new = json.loads(json.dumps(LOCK))
        new["packages"]["node_modules/c"] = {"version": "3.0.0", "integrity": "sha512-c"}
        _commit(repo, "frontend/package-lock.json", json.dumps(new))
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo, enabled=case != "disabled")
    assert fake.calls == [] and rec["status"] == "STALE" and rec["remedy"] == "REMEDY"
    expected = {"dirty": "local changes", "no_node_modules": "npm ci is never run automatically",
                "lock_changed": "lockfile package not installed (node_modules/c)", "disabled": "--no-ui-build"}[case]
    assert expected in rec["reason"] and "WARNING" in lines[0] and "REMEDY" in lines[0]


def test_a_matching_install_with_a_missing_optional_platform_package_counts_as_unchanged(repo):
    _install(repo)
    assert freshness.lockfile_matches_install(repo / "frontend")[0] is True
    hidden = repo / "frontend" / "node_modules" / ".package-lock.json"
    data = json.loads(hidden.read_text(encoding="utf-8"))
    data["packages"]["node_modules/a"]["version"] = "0.9.0"
    hidden.write_text(json.dumps(data), encoding="utf-8")
    ok, why = freshness.lockfile_matches_install(repo / "frontend")
    assert ok is False and "node_modules/a differs" in why


@pytest.mark.parametrize("fake_kw", [{"code": 1}, {"exc": subprocess.TimeoutExpired(["npm"], 1)}])
def test_a_failed_build_keeps_the_old_bundle_and_reports_stale(settings, repo, monkeypatch, fake_kw):
    _stamp(repo, "f" * 40)
    _install(repo)
    fake = FakeBuild(repo, **fake_kw)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert len(fake.calls) == 1 and rec["status"] == "STALE" and "rebuild failed" in rec["reason"]
    assert ("exited with 1" in rec["reason"]) or ("timed out" in rec["reason"])


@pytest.mark.parametrize("stamp", ["head", None])
def test_a_fresh_or_unverified_bundle_is_never_rebuilt(settings, repo, monkeypatch, stamp):
    if stamp:
        _stamp(repo, _head_tree(repo))
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert fake.calls == [] and lines == [] and rec["status"] == ("FRESH" if stamp else "UNVERIFIED")


def _serve(settings, repo: Path, monkeypatch, *flags: str) -> tuple[Any, dict[str, Any]]:
    import uvicorn
    from typer.testing import CliRunner

    from aeos import cli
    from aeos.core import paths

    started: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.update(k))
    monkeypatch.setattr(cli, "ensure_database", lambda: None)
    monkeypatch.setattr(paths, "repo_root", lambda: repo)
    res = CliRunner().invoke(cli.app, ["serve", "--release", "checkout", "--port", "18997", *flags])
    return res, started


def test_serve_still_starts_after_a_failed_build_and_health_says_stale(settings, repo, monkeypatch):
    _stamp(repo, "f" * 40)
    _install(repo)
    fake = FakeBuild(repo, code=2)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    monkeypatch.setattr(freshness, "WHICH", lambda n: f"/fake/bin/{n}")
    res, started = _serve(settings, repo, monkeypatch)
    assert res.exit_code == 0, res.output
    assert started["host"] == "127.0.0.1" and len(fake.calls) == 1
    assert "Serving the old bundle" in res.output
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        ui = c.get("/api/health").json()["ui_build"]
    assert ui["status"] == "STALE" and "exited with 2" in ui["reason"]


def test_serve_with_no_ui_build_does_not_build_and_health_says_why(settings, repo, monkeypatch):
    _stamp(repo, "f" * 40)
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    res, started = _serve(settings, repo, monkeypatch, "--no-ui-build")
    assert res.exit_code == 0, res.output
    assert started and fake.calls == []
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        ui = c.get("/api/health").json()["ui_build"]
    assert ui["status"] == "STALE" and "--no-ui-build" in ui["reason"]


def test_health_without_a_serve_record_says_not_recorded(settings):
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        assert c.get("/api/health").json()["ui_build"]["status"] == "NOT_RECORDED"


# ----------------------------------------------------------------------------------------- cache headers


@pytest.fixture
def web(settings, tmp_path, monkeypatch):
    from aeos.api import app as app_mod

    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AEOS UI</title>", encoding="utf-8")
    (dist / "assets" / "index-3f9a1c2b.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "aeos-build.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_mod, "_frontend_dist", lambda: dist)
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        yield c


def test_index_and_the_spa_fallback_revalidate(web):
    for path in ("/", "/projects/abc", "/aeos-build.json"):
        r = web.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path


def test_hashed_assets_are_immutable_and_a_missing_one_is_404(web):
    r = web.get("/assets/index-3f9a1c2b.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    r = web.get("/assets/index-deadbeef.js")
    assert r.status_code == 404 and "AEOS UI" not in r.text and r.json()["error"]["code"] == "NOT_FOUND"
    assert r.headers["cache-control"] == "no-cache"


def test_api_responses_keep_no_store(web):
    assert web.get("/api/health").headers["cache-control"] == "no-store"
    assert web.get("/api/no-such-route").headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------------------------- doctor: code_drift


def test_backend_drift_after_start_asks_for_a_restart(settings, db, repo):
    freshness.record_serve_start(settings, repo, ui_build=None)
    head = _commit(repo, "backend/app.py", "V = 2\n")
    res = _doctor(settings, db, "runtime.code_drift", repo)
    assert res.status == "CONFIGURED" and res.error_code == "CODE_DRIFT"
    assert f"restart aeos serve to load {head[:12]}" in res.summary
    assert res.observed_evidence["changed"] == ["backend/app.py"]


def test_docs_only_drift_and_the_same_commit_stay_verified(settings, db, repo):
    freshness.record_serve_start(settings, repo, ui_build=None)
    assert _doctor(settings, db, "runtime.code_drift", repo).status == "VERIFIED"
    _commit(repo, "docs/a.md", "b\n")
    _commit(repo, "frontend/src/main.ts", "export const v = 3;\n")
    res = _doctor(settings, db, "runtime.code_drift", repo)
    assert res.status == "VERIFIED" and "di luar backend" in res.summary


def test_no_running_serve_is_not_applicable(settings, db, repo):
    assert _doctor(settings, db, "runtime.code_drift", repo).status == "NOT_APPLICABLE"
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                          check=True)
    freshness.run_file(settings).parent.mkdir(parents=True, exist_ok=True)
    freshness.run_file(settings).write_text(json.dumps({"pid": int(proc.stdout), "create_time": 1.0,
                                                        "commit": "a" * 40, "repo": str(repo)}), encoding="utf-8")
    _commit(repo, "backend/app.py", "V = 9\n")
    res = _doctor(settings, db, "runtime.code_drift", repo)
    assert res.status == "NOT_APPLICABLE" and "not running" in res.summary


def test_the_backend_deps_fingerprint_follows_requirements_lock(settings, db, repo):
    load_all()
    spec = REGISTRY["runtime.backend_deps"]
    ctx = make_context(settings, db, runner=base_runner(), repo=repo)
    (repo / "requirements.lock").write_text("a==1\n", encoding="utf-8")
    before = compute_fingerprint(spec, ctx)
    (repo / "requirements.lock").write_text("a==2\n", encoding="utf-8")
    assert compute_fingerprint(spec, ctx) != before


# ----------------------------------------------------------------------------------------- post-merge hook

HOOK = ROOT / "scripts" / "git-hooks" / "post-merge"


def _merge_with_hook(repo: Path, rel: str) -> subprocess.CompletedProcess[str]:
    shutil.copy2(HOOK, repo / ".git" / "hooks" / "post-merge")
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, rel, "changed\n")
    _git(repo, "checkout", "-q", "-")
    return subprocess.run(["git", "-C", str(repo), "-c", f"user.name={TEST_GIT_NAME}", "-c",
                           f"user.email={TEST_GIT_EMAIL}", "merge", "-q", "--no-edit", "topic"],
                          capture_output=True, text=True, check=False)


def test_the_post_merge_hook_reminds_to_restart_when_backend_changed(repo):
    proc = _merge_with_hook(repo, "backend/app.py")
    assert proc.returncode == 0
    assert "frontend/backend changed: restart ./aeos serve" in proc.stdout + proc.stderr


def test_the_post_merge_hook_is_silent_for_docs_only(repo):
    proc = _merge_with_hook(repo, "docs/a.md")
    assert proc.returncode == 0 and "restart" not in proc.stdout + proc.stderr


def test_the_post_merge_hook_is_executable_and_never_fails():
    text = HOOK.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and "exit 0" in text and "set -e" not in text
    index = subprocess.run(["git", "ls-files", "-s", "scripts/git-hooks/post-merge"], cwd=ROOT, capture_output=True,
                           text=True, check=False).stdout
    assert index.startswith("100755"), "commit the hook with the executable bit (git update-index --chmod=+x)"
