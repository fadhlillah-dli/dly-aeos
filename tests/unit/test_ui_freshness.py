"""R28 / LESSONS L23: what ``aeos serve`` serves stays current after a ``git pull``.

* Doctor ``runtime.frontend_dist`` is the existence check (critical: MISSING blocks CAN_OPEN_DASHBOARD, exit 1);
  ``runtime.frontend_fresh`` compares the build record with ``HEAD:frontend`` (FRONTEND_STALE when it differs,
  UNVERIFIED without a clean record, VERIFIED after a backend-only commit) and only DEGRADES the capability.
* ``aeos serve`` rebuilds a STALE, MISSING or UNVERIFIED bundle once when node_modules matches the lockfile and
  frontend/ is clean, never installs anything and never blocks the start; /api/health reports ``ui_build``. The build
  goes to frontend/dist.next and is swapped in only after success: a failed or killed build leaves dist untouched.
* Cache headers: index.html and the SPA fallback revalidate, hashed assets are immutable, a missing asset is 404.
* Doctor ``runtime.code_drift``: the running server's loaded commit against HEAD (backend paths only).

Tests never run a real npm build (tests/conftest.py refuses one; these tests inject a fake runner).
"""

from __future__ import annotations

import json
import os
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
    (r / ".gitignore").write_text("frontend/dist/\nfrontend/dist.next/\nfrontend/dist.prev/\nfrontend/node_modules/\n",
                                  encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    dist = r / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AEOS</title>", encoding="utf-8")
    return r


def _stamp(repo: Path, tree: str | None, clean: bool = True, dist: Path | None = None) -> None:
    (dist or repo / "frontend" / "dist").joinpath("aeos-build.json").write_text(
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
    res = _doctor(settings, db, "runtime.frontend_fresh", repo)
    assert res.status == "CONFIGURED" and res.error_code == "FRONTEND_STALE"
    assert f"bundle built from frontend tree ffffffffffff, HEAD has {_head_tree(repo)[:12]}" in res.summary
    assert "npm" in res.remediation["command"] and "run build" in res.remediation["command"]
    assert "Restart aeos serve" in res.remediation["operation"]
    # the existence check stays VERIFIED: the dashboard opens, with an older UI
    assert _doctor(settings, db, "runtime.frontend_dist", repo).status == "VERIFIED"


def test_a_backend_only_commit_stays_verified_and_keeps_the_fingerprint(settings, db, repo):
    _stamp(repo, _head_tree(repo))
    load_all()
    spec = REGISTRY["runtime.frontend_fresh"]
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
    res = _doctor(settings, db, "runtime.frontend_fresh", repo)
    assert res.status == "UNKNOWN" and res.error_code == "FRONTEND_BUILD_UNVERIFIED" and "UNVERIFIED" in res.summary
    _stamp(repo, _head_tree(repo), clean=False)
    res = _doctor(settings, db, "runtime.frontend_fresh", repo)
    assert res.status == "UNKNOWN" and "uncommitted" in res.summary


def test_without_a_bundle_freshness_is_not_applicable_and_existence_is_missing(settings, db, repo):
    shutil.rmtree(repo / "frontend" / "dist")
    assert _doctor(settings, db, "runtime.frontend_fresh", repo).status == "NOT_APPLICABLE"
    res = _doctor(settings, db, "runtime.frontend_dist", repo)
    assert res.status == "MISSING" and res.error_code == "FRONTEND_NOT_BUILT"


def _dashboard(settings, db, repo: Path) -> tuple[str, list[str], list[str]]:
    """(CAN_OPEN_DASHBOARD status, its reasons, the checks that make 'aeos doctor' exit 1) when every other check of
    the capability is ok and the two frontend checks really ran against *repo*."""
    from aeos.core.enums import Capability
    from aeos.core.timeutil import utcnow
    from aeos.doctor.capabilities import evaluate
    from aeos.doctor.cli import dashboard_critical_failures
    from tests.unit.test_doctor_capabilities import all_ok

    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    results = [_doctor(settings, db, cid, repo) for cid in ("runtime.frontend_dist", "runtime.frontend_fresh")]
    for r in results:
        rows[r.check_id].update(status=r.status, summary=r.summary)
    st = evaluate(Capability.CAN_OPEN_DASHBOARD, rows, now=utcnow(), fps={}, domain=None, session=None)
    return st.status, st.reasons, dashboard_critical_failures(results)


def test_a_stale_bundle_degrades_the_dashboard_and_never_blocks_it(settings, db, repo):
    """Bite: in round 1 FRONTEND_STALE sat in the critical tier, blocked CAN_OPEN_DASHBOARD and made 'aeos doctor'
    exit 1 although the dashboard opens; now it is DEGRADED with exit 0. Near miss: a missing index.html still
    blocks with exit 1."""
    _stamp(repo, "f" * 40)
    status, reasons, exit1 = _dashboard(settings, db, repo)
    assert status == "DEGRADED" and exit1 == [], (status, exit1)
    assert any(r.startswith("runtime.frontend_fresh: CONFIGURED - Bundle Control Center usang") for r in reasons)
    _stamp(repo, None)  # no record: UNVERIFIED is not a pass either, and not a block
    assert _dashboard(settings, db, repo)[0] == "DEGRADED"
    _stamp(repo, _head_tree(repo))
    assert _dashboard(settings, db, repo)[:1] == ("READY",)
    shutil.rmtree(repo / "frontend" / "dist")
    status, reasons, exit1 = _dashboard(settings, db, repo)
    assert status == "BLOCKED" and exit1 == ["runtime.frontend_dist"]
    assert any(r.startswith("runtime.frontend_dist: MISSING") for r in reasons), reasons


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

OLD_INDEX = "<!doctype html><title>AEOS</title>"  # the bundle the repo fixture built
NEXT = ["/fake/bin/npm", "run", "build", "--", "--outDir", "dist.next"]


class FakeBuild:
    """Stands in for 'npm run build -- --outDir <dir>'. Like Vite it first empties the outDir it is given (renderStart),
    writes assets, then index.html and the build record. A failing run (exit code or exception) stops after the assets:
    a partial outDir without index.html, as a killed or failing Vite build leaves it."""

    def __init__(self, repo: Path, code: int = 0, exc: BaseException | None = None, tree: str | None = None) -> None:
        self.repo, self.code, self.exc, self.tree, self.calls = repo, code, exc, tree, []

    def __call__(self, argv: list[str], cwd: Path, timeout: float, log_path: Path) -> int:
        self.calls.append(argv)
        out = cwd / (argv[argv.index("--outDir") + 1] if "--outDir" in argv else "dist")
        if out.exists():
            shutil.rmtree(out)
        (out / "assets").mkdir(parents=True)
        (out / "assets" / "index-new.js").write_text("new", encoding="utf-8")
        if self.exc is not None:
            raise self.exc
        if self.code != 0:
            return self.code
        (out / "index.html").write_text("<!doctype html><title>NEW</title>", encoding="utf-8")
        _stamp(self.repo, self.tree or _head_tree(self.repo), dist=out)
        return 0


class FlakyRename:
    """os.rename that raises PermissionError (a locked file on Windows) on the given call numbers (1-based)."""

    def __init__(self, *fail_on: int) -> None:
        self.fail_on, self.calls = set(fail_on), []

    def __call__(self, src: Path, dst: Path) -> None:
        self.calls.append((Path(src).name, Path(dst).name))
        if len(self.calls) in self.fail_on:
            raise PermissionError(13, "The process cannot access the file because it is being used", str(src))
        os.rename(src, dst)


def _ensure(settings, repo: Path, **kw: Any) -> tuple[dict[str, Any], list[str]]:
    lines: list[str] = []
    rec = freshness.ensure_ui_fresh(settings, repo, remedy="REMEDY", which=lambda n: f"/fake/bin/{n}",
                                    echo=lines.append, **kw)
    return rec, lines


def _index(repo: Path) -> str | None:
    index = repo / "frontend" / "dist" / "index.html"
    return index.read_text(encoding="utf-8") if index.is_file() else None


def _no_leftovers(repo: Path) -> bool:
    return not (repo / "frontend" / "dist.next").exists() and not (repo / "frontend" / "dist.prev").exists()


def _make_state(repo: Path, state: str) -> None:
    if state == "STALE":
        _stamp(repo, "f" * 40)
    elif state == "UNVERIFIED_DIRTY":
        _stamp(repo, _head_tree(repo), clean=False)
    elif state == "MISSING":
        shutil.rmtree(repo / "frontend" / "dist")
    # UNVERIFIED (no record): the fixture's dist has no aeos-build.json


STATES = ["STALE", "MISSING", "UNVERIFIED", "UNVERIFIED_DIRTY"]


@pytest.mark.parametrize("state", STATES)
def test_a_stale_missing_or_unverified_bundle_is_built_into_dist_next_and_swapped_in(settings, repo, monkeypatch,
                                                                                     state):
    _make_state(repo, state)
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert fake.calls == [NEXT]
    assert rec["status"] == "REBUILT" and rec["previous_status"] == state.split("_")[0], rec
    assert len(lines) == 1 and "running 'npm run build' once into frontend/dist.next" in lines[0]
    assert "NEW" in str(_index(repo)) and _no_leftovers(repo)
    assert freshness.bundle_state(settings, repo)["status"] == "FRESH"


@pytest.mark.parametrize("state", ["STALE", "MISSING", "UNVERIFIED"])
@pytest.mark.parametrize("case", ["dirty", "no_node_modules", "lock_changed", "disabled"])
def test_a_bundle_is_not_rebuilt_when_it_is_not_safe(settings, repo, monkeypatch, case, state):
    _make_state(repo, state)
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
    assert fake.calls == [] and rec["status"] == state and rec["remedy"] == "REMEDY"
    expected = {"dirty": "local changes", "no_node_modules": "npm ci is never run automatically",
                "lock_changed": "lockfile package not installed (node_modules/c)", "disabled": "--no-ui-build"}[case]
    assert expected in rec["reason"] and "WARNING" in lines[0] and "REMEDY" in lines[0]
    serving = "No bundle to serve" if state == "MISSING" else "Serving the old bundle"
    assert serving in lines[0]


def test_a_matching_install_with_a_missing_optional_platform_package_counts_as_unchanged(repo):
    _install(repo)
    assert freshness.lockfile_matches_install(repo / "frontend")[0] is True
    hidden = repo / "frontend" / "node_modules" / ".package-lock.json"
    data = json.loads(hidden.read_text(encoding="utf-8"))
    data["packages"]["node_modules/a"]["version"] = "0.9.0"
    hidden.write_text(json.dumps(data), encoding="utf-8")
    ok, why = freshness.lockfile_matches_install(repo / "frontend")
    assert ok is False and "node_modules/a differs" in why


@pytest.mark.parametrize(("fake_kw", "why"), [
    ({"code": 1}, "exited with 1"),
    ({"exc": subprocess.TimeoutExpired(["npm"], 1)}, "timed out"),
    ({"tree": "e" * 40}, "the build finished but frontend/dist.next is STALE"),
])
def test_a_failed_or_killed_build_leaves_the_old_bundle_untouched(settings, repo, monkeypatch, fake_kw, why):
    _stamp(repo, "f" * 40)
    _install(repo)
    fake = FakeBuild(repo, **fake_kw)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert len(fake.calls) == 1 and rec["status"] == "STALE" and "rebuild failed" in rec["reason"]
    assert why in rec["reason"] and "Serving the old bundle" in lines[-1]
    assert _index(repo) == OLD_INDEX and _no_leftovers(repo)
    assert json.loads((repo / "frontend" / "dist" / "aeos-build.json").read_text(encoding="utf-8"))[
        "frontend_tree"] == "f" * 40


def test_a_build_into_dist_itself_is_reported_missing_never_serving_the_old_bundle(settings, repo, monkeypatch):
    """Bite for the round-1 design: a build whose outDir is dist itself (Vite empties it first) and then fails leaves
    no index.html. The health record must then say MISSING with the remedy, not 'serving the old bundle'."""
    _stamp(repo, "f" * 40)
    _install(repo)
    monkeypatch.setattr(freshness, "NEXT_DIR", "dist")
    monkeypatch.setattr(freshness, "BUILD_RUNNER", FakeBuild(repo, code=1))
    rec, lines = _ensure(settings, repo)
    assert _index(repo) is None
    assert rec["status"] == "MISSING" and rec["remedy"] == "REMEDY"
    assert "No bundle to serve" in lines[-1] and "Serving the old bundle" not in lines[-1]


@pytest.mark.parametrize(("fail_on", "status", "why"), [
    ((1,), "STALE", "could not move frontend/dist aside (PermissionError"),
    ((2,), "STALE", "could not move frontend/dist.next to frontend/dist (PermissionError"),
    ((2, 3), "MISSING", "restoring the old bundle failed too (PermissionError"),
])
def test_a_rename_that_fails_on_a_locked_file_is_reported_and_restored(settings, repo, monkeypatch, fail_on, status,
                                                                      why):
    _stamp(repo, "f" * 40)
    _install(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", FakeBuild(repo))
    flaky = FlakyRename(*fail_on)
    monkeypatch.setattr(freshness, "RENAME", flaky)
    rec, lines = _ensure(settings, repo)
    assert rec["status"] == status and why in rec["reason"], rec
    assert rec["remedy"] == "REMEDY" and "WARNING" in lines[-1]
    if status == "STALE":
        assert _index(repo) == OLD_INDEX and _no_leftovers(repo)
        assert "Serving the old bundle" in lines[-1]
    else:
        assert _index(repo) is None and "it is in frontend/dist.prev" in rec["reason"]
        assert "No bundle to serve" in lines[-1]
        # the next start puts the old bundle back first, then rebuilds and swaps normally
        monkeypatch.setattr(freshness, "RENAME", os.rename)
        again, _ = _ensure(settings, repo)
        assert again["status"] == "REBUILT" and "NEW" in str(_index(repo)) and _no_leftovers(repo)


def test_an_old_bundle_that_cannot_be_deleted_after_the_swap_is_a_note_not_a_failure(settings, repo, monkeypatch):
    _stamp(repo, "f" * 40)
    _install(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", FakeBuild(repo))
    real = freshness._remove_tree

    def locked(path: Path) -> str | None:
        return "PermissionError: locked" if path.name == "dist.prev" and path.exists() else real(path)

    monkeypatch.setattr(freshness, "_remove_tree", locked)
    rec, _ = _ensure(settings, repo)
    assert rec["status"] == "REBUILT" and "dist.prev could not be deleted" in rec["note"]
    assert "NEW" in str(_index(repo))


def test_a_fresh_bundle_is_never_rebuilt(settings, repo, monkeypatch):
    _stamp(repo, _head_tree(repo))
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert fake.calls == [] and lines == [] and rec["status"] == "FRESH"


@pytest.mark.parametrize("missing", [False, True])
def test_a_tree_without_git_is_never_rebuilt(settings, tmp_path, monkeypatch, missing):
    root = tmp_path / "plain"
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "package-lock.json").write_text(json.dumps(LOCK), encoding="utf-8")
    if missing:
        (root / "frontend" / "dist").rmdir()
    else:
        (root / "frontend" / "dist" / "index.html").write_text(OLD_INDEX, encoding="utf-8")
    _install(root)
    fake = FakeBuild(root)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, _ = _ensure(settings, root)
    assert fake.calls == [] and rec["status"] == ("MISSING" if missing else "UNVERIFIED")


def test_a_release_install_is_never_rebuilt(settings, monkeypatch):
    root = settings.layout.released_controller / "0.1.0-abcdef123456"
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text(OLD_INDEX, encoding="utf-8")
    _install(root)
    fake = FakeBuild(root)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, _ = _ensure(settings, root)
    assert fake.calls == [] and rec["status"] == "UNVERIFIED" and rec["basis"] == "release"


def _interrupted_swap(repo: Path) -> None:
    """What a swap stopped between its two renames leaves: the old (STALE) bundle in dist.prev, no dist."""
    _stamp(repo, "f" * 40)
    os.rename(repo / "frontend" / "dist", repo / "frontend" / "dist.prev")


def test_a_restored_bundle_whose_rebuild_then_fails_is_stale_never_missing(settings, repo, monkeypatch):
    """Round-3 bite: the leftover dist.prev is put back, the rebuild exits 1; the record must describe dist as it is
    now (the restored, STALE bundle is served), not the MISSING status from before the restore."""
    _interrupted_swap(repo)
    _install(repo)
    fake = FakeBuild(repo, code=1)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    rec, lines = _ensure(settings, repo)
    assert len(fake.calls) == 1 and _index(repo) == OLD_INDEX
    assert rec["status"] == "STALE", rec
    assert "exited with 1" in rec["reason"] and "restored from frontend/dist.prev" in rec["note"]
    assert rec["bundle_frontend_tree"] == "f" * 40
    assert "Serving the old bundle" in lines[-1] and "No bundle to serve" not in lines[-1]


@pytest.mark.parametrize("case", ["disabled", "no_npm", "dirty", "no_node_modules"])
def test_a_leftover_old_bundle_is_restored_even_when_no_rebuild_is_allowed(settings, repo, monkeypatch, case):
    """Round-3 bite: restoring dist.prev needs no npm, so it happens before the safety gate; otherwise every page
    returned 503 FRONTEND_NOT_BUILT although a valid bundle was on disk."""
    _interrupted_swap(repo)
    if case != "no_node_modules":
        _install(repo)
    if case == "dirty":
        (repo / "frontend" / "src" / "draft.ts").write_text("x\n", encoding="utf-8")
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    lines: list[str] = []
    rec = freshness.ensure_ui_fresh(settings, repo, remedy="REMEDY", enabled=case != "disabled", echo=lines.append,
                                    which=(lambda n: None) if case == "no_npm" else (lambda n: f"/fake/bin/{n}"))
    assert _index(repo) == OLD_INDEX, f"the old bundle in dist.prev was not restored: {rec}"
    assert fake.calls == [] and not (repo / "frontend" / "dist.prev").exists()
    assert rec["status"] == "STALE" and "not rebuilt" in rec["reason"], rec
    assert "restored from frontend/dist.prev" in rec["note"] and "Serving the old bundle" in lines[0]


def test_a_leftover_without_index_html_is_not_restored(settings, repo, monkeypatch):
    """Near miss: a dist.prev without index.html is not a bundle; dist stays MISSING (nothing is invented)."""
    _interrupted_swap(repo)
    (repo / "frontend" / "dist.prev" / "index.html").unlink()
    rec, _ = _ensure(settings, repo, enabled=False)
    assert rec["status"] == "MISSING" and _index(repo) is None and "note" not in rec


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


def test_serve_builds_a_missing_bundle_and_health_says_rebuilt(settings, repo, monkeypatch):
    shutil.rmtree(repo / "frontend" / "dist")
    _install(repo)
    fake = FakeBuild(repo)
    monkeypatch.setattr(freshness, "BUILD_RUNNER", fake)
    monkeypatch.setattr(freshness, "WHICH", lambda n: f"/fake/bin/{n}")
    res, started = _serve(settings, repo, monkeypatch)
    assert res.exit_code == 0, res.output
    assert started and fake.calls == [NEXT] and "NEW" in str(_index(repo))
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        ui = c.get("/api/health").json()["ui_build"]
    assert ui["status"] == "REBUILT" and ui["previous_status"] == "MISSING"


def test_the_repository_ignores_the_temporary_build_directories():
    """Without this, the Vite build record of a build into dist.next says clean=false (git status sees dist.next)
    and a leftover dist.prev makes frontend/ dirty, so serve would never rebuild again."""
    for rel in ("frontend/dist.next/index.html", "frontend/dist.prev/index.html"):
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT, check=False)
        assert proc.returncode == 0, f"{rel} is not ignored by .gitignore"


def test_health_without_a_serve_record_says_not_recorded(settings):
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        assert c.get("/api/health").json()["ui_build"]["status"] == "NOT_RECORDED"


# ----------------------------------------------------------------------------------------- /api/health freshness


class GitCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.real = freshness._git

    def __call__(self, repo: Path, *args: str, timeout: float = 30) -> str | None:
        self.calls.append(args)
        return self.real(repo, *args, timeout=timeout)


@pytest.fixture
def health(settings, repo, monkeypatch):
    """GET /api/health of a server started from *repo* (its dist is the served bundle), with a controllable clock."""
    from aeos.api import app as app_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(freshness, "CLOCK", lambda: clock["now"])
    monkeypatch.setattr(app_mod, "_frontend_dist", lambda: repo / "frontend" / "dist")
    counter = GitCounter()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        def get() -> dict[str, Any]:
            monkeypatch.setattr(freshness, "_git", counter)
            try:
                return c.get("/api/health").json()
            finally:
                monkeypatch.setattr(freshness, "_git", counter.real)
        yield get, clock, counter


def test_health_freshness_reports_the_served_stamp_and_current_code(settings, repo, health):
    get, _, _ = health
    _stamp(repo, _head_tree(repo))
    freshness.record_serve_start(settings, repo, ui_build=None)
    body = get()
    block = body["freshness"]
    assert block["served_bundle"]["frontend_tree"] == _head_tree(repo)
    assert block["code_drift"]["status"] == "CURRENT" and block["max_age_seconds"] == 30
    assert "changed" not in block["code_drift"]  # file names stay in the Doctor
    assert body["ui_build"] and body["controller"]  # additive: the existing fields stay


def test_health_freshness_reports_backend_drift_with_the_loaded_commit(settings, repo, health):
    get, _, _ = health
    loaded = _git(repo, "rev-parse", "HEAD")
    freshness.record_serve_start(settings, repo, ui_build=None)
    _commit(repo, "backend/app.py", "V = 3\n")
    drift = get()["freshness"]["code_drift"]
    assert drift["status"] == "DRIFT" and drift["loaded_commit"] == loaded and drift["changed_count"] == 1


def test_health_freshness_is_cached_one_git_call_per_window(settings, repo, health):
    """Bite: health is refetched on every focus; within the 30 s window it makes no git call at all."""
    get, clock, counter = health
    freshness.record_serve_start(settings, repo, ui_build=None)
    first = get()["freshness"]
    assert counter.calls == [("rev-parse", "HEAD")]
    clock["now"] += 29
    _commit(repo, "backend/app.py", "V = 4\n")
    assert get()["freshness"] == first and len(counter.calls) == 1  # cached: same block, no git call
    clock["now"] += 2
    assert get()["freshness"]["code_drift"]["status"] == "DRIFT" and len(counter.calls) == 3  # rev-parse + diff


def test_health_freshness_without_a_serve_record_says_not_recorded(settings, health):
    get, _, counter = health
    block = get()["freshness"]
    assert block["code_drift"]["status"] == "NOT_RECORDED" and counter.calls == []
    assert block["served_bundle"]["frontend_tree"] is None  # the fixture bundle has no build record


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


def _merge_with_hook(repo: Path, rel: str) -> subprocess.CompletedProcess[bytes]:
    shutil.copy2(HOOK, repo / ".git" / "hooks" / "post-merge")
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, rel, "changed\n")
    _git(repo, "checkout", "-q", "-")
    return subprocess.run(["git", "-C", str(repo), "-c", f"user.name={TEST_GIT_NAME}", "-c",
                           f"user.email={TEST_GIT_EMAIL}", "merge", "-q", "--no-edit", "topic"],
                          capture_output=True, check=False)


HINT = b"post-merge: frontend/backend changed: restart ./aeos serve (Windows: .\\aeos.ps1 serve)\n"


def _hint_problem(out: bytes) -> str | None:
    """What is wrong with the hook's reminder in *out* (the exact bytes the hook printed), or None."""
    if b"\x07" in out:
        return "the Windows hint lost its backslash: sh echo read '\\a' of '.\\aeos.ps1' as a BEL character"
    if HINT not in out:
        return f"the reminder is not exactly {HINT!r}"
    return None


def test_the_hint_checker_bites_on_the_bel_and_passes_the_exact_text():
    """Bite: the round-1 hook printed '.<BEL>eos.ps1' (echo interpreted the backslash). Near miss: the exact text."""
    bel = HINT.replace(b"\\a", b"\x07")
    assert _hint_problem(bel) == ("the Windows hint lost its backslash: sh echo read '\\a' of '.\\aeos.ps1' as a BEL "
                                  "character")
    assert _hint_problem(b"post-merge: frontend/backend changed: restart ./aeos serve\n").startswith(
        "the reminder is not exactly")
    assert _hint_problem(b"noise\n" + HINT) is None


def test_the_post_merge_hook_reminds_to_restart_when_backend_changed(repo):
    proc = _merge_with_hook(repo, "backend/app.py")
    assert proc.returncode == 0
    out = proc.stdout + proc.stderr  # git runs hooks with stdout on stderr
    assert _hint_problem(out) is None, (_hint_problem(out), out)


def test_the_post_merge_hook_prints_with_printf_never_echo():
    code = [ln.strip() for ln in HOOK.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if ln.startswith("echo") or " echo " in ln], code
    assert "printf '%s\\n' 'post-merge: frontend/backend changed: restart ./aeos serve (Windows: .\\aeos.ps1 serve)'" \
        in code


def test_the_post_merge_hook_is_linted_by_shellcheck_like_pre_push():
    import importlib.util

    spec = importlib.util.spec_from_file_location("aeos_script_gates_ui_freshness", ROOT / "scripts" / "gates.py")
    assert spec is not None and spec.loader is not None
    gates = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gates
    spec.loader.exec_module(gates)
    assert gates.SHELLCHECK_COMMAND.split()[-2:] == ["scripts/git-hooks/pre-push", "scripts/git-hooks/post-merge"]
    assert gates.shell_files(ROOT)[-2:] == ["scripts/git-hooks/pre-push", "scripts/git-hooks/post-merge"]


def test_the_post_merge_hook_is_silent_for_docs_only(repo):
    proc = _merge_with_hook(repo, "docs/a.md")
    assert proc.returncode == 0 and b"restart" not in proc.stdout + proc.stderr


def test_the_post_merge_hook_is_executable_and_never_fails():
    text = HOOK.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n") and "exit 0" in text and "set -e" not in text
    index = subprocess.run(["git", "ls-files", "-s", "scripts/git-hooks/post-merge"], cwd=ROOT, capture_output=True,
                           text=True, check=False).stdout
    assert index.startswith("100755"), "commit the hook with the executable bit (git update-index --chmod=+x)"
