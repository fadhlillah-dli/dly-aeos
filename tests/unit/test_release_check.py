"""scripts/release_check.py: the release tag check, on temporary git repositories (never the real tree).

Incident class: a release tagged with one version file out of date, a fragment left behind, missing release notes,
or a tag that is not on main. Each check has a violating fixture that is red (and names its own finding) and the
consistent repository is green."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
SCRIPT = ROOT / "scripts" / "release_check.py"
HAS_GIT = subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0
pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_release_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load()
VERSION = "1.2.3"


def sources(version: str = VERSION) -> dict[str, str]:
    return {
        "pyproject.toml": f'[project]\nname = "demo"\nversion = "{version}"\n',
        "frontend/package.json": json.dumps({"name": "demo", "version": version}),
        "frontend/package-lock.json": json.dumps({"packages": {"": {"name": "demo", "version": version}}}),
        "CITATION.cff": f'cff-version: 1.2.0\ntitle: Demo\nversion: "{version}"\n',
        "backend/aeos/__init__.py": f'"""Demo."""\n\n__version__ = "{version}"\n',
    }


def consistent_files() -> dict[str, str]:
    files = sources()
    files.update({
        "CHANGELOG.md": f"# Changelog\n\n## [Unreleased]\n\n## [{VERSION}] - 2026-01-01\n\n### Added\n\n- **x.** y\n",
        "changelog.d/README.md": "# Fragments\n",
        f"docs/releases/v{VERSION}.md": f"# Demo v{VERSION}\n",
    })
    return files


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, encoding="utf-8",
                          check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(tmp_path: Path, files: dict[str, str], tag: str = f"v{VERSION}", on_main: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "chore: root")
    if not on_main:
        git(repo, "switch", "-q", "-c", "release")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: release")
    git(repo, "tag", tag)
    if not on_main:
        git(repo, "switch", "-q", "main")
    return repo


def check(repo: Path, tag: str = f"v{VERSION}"):
    results = rc.check_tag(repo, tag, "main")
    return [msg for ok, msg in results if not ok], [msg for ok, msg in results if ok]


def test_a_consistent_repository_is_green(tmp_path):
    failed, passed = check(make_repo(tmp_path, consistent_files()))
    assert failed == []
    assert len(passed) == 1 + len(rc.VERSION_SOURCES) + 4  # tag, every version source, changelog, fragments, notes, main


@pytest.mark.parametrize("rel", [rel for rel, _ in rc.VERSION_SOURCES])
def test_one_version_source_that_differs_is_red_and_names_the_file(tmp_path, rel):
    files = consistent_files()
    files[rel] = sources("1.2.2")[rel]
    failed, _ = check(make_repo(tmp_path, files))
    assert failed == [f"{rel}: version is 1.2.2, the tag says {VERSION}"]


def test_version_source_list_covers_the_five_known_files():
    assert [rel for rel, _ in rc.VERSION_SOURCES] == [
        "pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "CITATION.cff",
        "backend/aeos/__init__.py"]


def test_a_leftover_fragment_is_red_and_names_it(tmp_path):
    files = consistent_files()
    files["changelog.d/leftover-fix.md"] = "### Fixed\n\n- **x.** y\n"
    failed, _ = check(make_repo(tmp_path, files))
    assert failed == ["changelog.d/: fragments left behind: leftover-fix.md"]


def test_changelog_without_the_version_heading_is_red(tmp_path):
    files = consistent_files()
    files["CHANGELOG.md"] = "# Changelog\n\n## [Unreleased]\n\n## [1.2.30] - 2026-01-01\n"  # near miss: other version
    failed, _ = check(make_repo(tmp_path, files))
    assert failed == [f"CHANGELOG.md: no '## [{VERSION}]' heading at the tagged commit"]


def test_missing_release_notes_are_red(tmp_path):
    files = consistent_files()
    del files[f"docs/releases/v{VERSION}.md"]
    failed, _ = check(make_repo(tmp_path, files))
    assert failed == [f"docs/releases/v{VERSION}.md: missing at the tagged commit"]


def test_a_tag_that_is_not_on_main_is_red(tmp_path):
    failed, _ = check(make_repo(tmp_path, consistent_files(), on_main=False))
    assert len(failed) == 1 and "is NOT an ancestor of main" in failed[0]


def test_a_missing_tag_and_a_malformed_tag_are_red(tmp_path):
    repo = make_repo(tmp_path, consistent_files())
    assert "does not exist" in check(repo, "v9.9.9")[0][0]
    assert "is not vMAJOR.MINOR.PATCH" in check(repo, "1.2.3")[0][0]


def test_files_are_read_from_the_tagged_commit_not_the_working_tree(tmp_path):
    repo = make_repo(tmp_path, consistent_files())
    (repo / "pyproject.toml").write_text(sources("9.9.9")["pyproject.toml"], encoding="utf-8")  # dirty tree
    failed, _ = check(repo)
    assert failed == []


def test_an_unparsable_source_is_red_not_a_crash(tmp_path):
    files = consistent_files()
    files["frontend/package.json"] = "{not json"
    failed, _ = check(make_repo(tmp_path, files))
    assert failed == ["frontend/package.json: declares no readable version"]


def test_command_line_ends_with_the_verdict_line_and_the_exit_code(tmp_path):
    green = make_repo(tmp_path, consistent_files())
    proc = subprocess.run([sys.executable, str(SCRIPT), "--tag", f"v{VERSION}", "--main", "main", "--root", str(green)],
                          capture_output=True, text=True, encoding="utf-8", check=False)
    assert proc.returncode == 0 and proc.stdout.splitlines()[-1] == "RELEASE_CHECK: PASS"
    proc.stdout.encode("ascii")
    red = subprocess.run([sys.executable, str(SCRIPT), "--tag", "v9.9.9", "--main", "main", "--root", str(green)],
                         capture_output=True, text=True, encoding="utf-8", check=False)
    assert red.returncode == 1 and red.stdout.splitlines()[-1] == "RELEASE_CHECK: FAIL"


def test_run_prints_one_verdict_line_last(tmp_path):
    repo = make_repo(tmp_path, consistent_files())
    buf = io.StringIO()
    assert rc.run(["--tag", f"v{VERSION}", "--main", "main", "--root", str(repo)], buf) == 0
    lines = buf.getvalue().splitlines()
    assert lines[-1] == "RELEASE_CHECK: PASS" and sum(1 for ln in lines if ln.startswith("RELEASE_CHECK:")) == 1
