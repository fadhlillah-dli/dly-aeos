"""Changelog fragments (scripts/changelog.py, changelog.d/): bite tests.

Every check has a violating fixture that must be red AND a near-miss legitimate fixture that must be green.
Fixtures live in tmp_path; the working tree is never touched. Git runs hermetically (no global or system config,
no network)."""

from __future__ import annotations

import copy
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from aeos.core.paths import repo_root
from tests.unit.test_repository_hygiene import assert_changelog_structure

ROOT = repo_root()


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_changelog_bite", ROOT / "scripts" / "changelog.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cl = _load()

GOOD = "### Fixed\n\n- **Title.** What changed, why, E2.\n"

BASE_LOG = """# Changelog

Intro.

## [Unreleased]

### Added

- **Old added.** Already there.

### Fixed

- **Old fixed.** Already there.

## [0.1.0] - 2026-01-01

### Added

- **Released.** Text.

[unreleased]: https://example.invalid/compare/v0.1.0...HEAD
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _repo(tmp_path: Path, log: str = BASE_LOG) -> Path:
    root = tmp_path / "repo"
    _write(root / "CHANGELOG.md", log)
    _write(root / "changelog.d" / "README.md", "# Changelog fragments\n")
    return root


def _frag(root: Path, name: str, text: str) -> Path:
    return _write(root / "changelog.d" / name, text)


def _run(root: Path, *args: str) -> int:
    return cl.main(["--root", str(root), *args])


# ------------------------------------------------------------------------------------------ fragment format
@pytest.mark.parametrize("name, text, needle", [
    ("no-heading.md", "- **Title.** Text.\n", "line 1"),
    ("two-headings.md", "### Added\n\n- **Title.** Text.\n\n### Fixed\n\n- **Other.** Text.\n", "only one heading"),
    ("conflict.md", "### Added\n\n<<<<<<< HEAD\n- **Title.** Text.\n=======\n- **Other.** Text.\n>>>>>>> b\n",
     "conflict marker"),
    ("bad-section.md", "### Improved\n\n- **Title.** Text.\n", "unknown section"),
    ("no-blank.md", "### Added\n- **Title.** Text.\n", "line 2"),
    ("no-bullet.md", "### Added\n\nJust prose.\n", "bullet"),
    ("plain-bullet.md", "### Added\n\n- Title without bold.\n", "line 3"),
    ("trailing.md", "### Added\n\n- **Title.** Text. \n", "trailing whitespace"),
    ("trailing-tab.md", "### Added\n\n- **Title.** Text.\t\n", "trailing whitespace"),
    ("h2.md", "### Added\n\n- **Title.** Text.\n\n## Extra\n", "only one heading"),
    ("empty.md", "", "line 1"),
    ("fenced-h3.md", "### Fixed\n\n- **Code.** Example:\n\n  ```\n### Added\n  ```\n", "only one heading"),
    ("fenced-h2.md", "### Fixed\n\n- **Code.** Example:\n\n```\n## Extra\n```\n", "only one heading"),
    ("link-def.md", "### Added\n\n- **Linked.** See [the ADR][adr].\n\n[adr]: docs/decisions/x.md\n",
     "link-reference definition"),
    ("fenced-link-def.md", "### Added\n\n- **Linked.** Example:\n\n  ```\n[adr]: docs/x.md\n  ```\n",
     "link-reference definition"),
])
def test_malformed_fragment_is_red(tmp_path, capsys, name, text, needle):
    root = _repo(tmp_path)
    _frag(root, name, text)
    assert _run(root, "--check") == 1
    out = capsys.readouterr().out
    assert f"changelog.d/{name}" in out and needle in out, out


def test_non_utf8_fragment_is_red(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / "changelog.d" / "latin.md").write_bytes(b"### Added\n\n- **T\xe9st.** Text.\n")
    assert _run(root, "--check") == 1
    assert "not valid UTF-8" in capsys.readouterr().out


def test_stray_files_in_the_fragment_directory_are_red(tmp_path, capsys):
    root = _repo(tmp_path)
    _frag(root, "notes.txt", GOOD)
    (root / "changelog.d" / "sub").mkdir()
    assert _run(root, "--check") == 1
    out = capsys.readouterr().out
    assert "notes.txt" in out and "sub" in out


def test_hidden_junk_files_are_ignored_but_a_hidden_md_fragment_is_not(tmp_path, capsys):
    root = _repo(tmp_path)
    _frag(root, "ok.md", GOOD)
    for junk in (".DS_Store", ".ok.md.swp", ".gitkeep"):
        _frag(root, junk, "binary junk")
    assert _run(root, "--check") == 0  # near-miss: gitignored OS and editor files are green
    assert _run(root, "--assemble") == 0
    assert (root / "changelog.d" / ".DS_Store").is_file()  # and they are left alone
    _frag(root, ".hidden.md", "no heading\n")  # a hidden fragment is still a fragment, so it is checked
    assert _run(root, "--check") == 1
    assert ".hidden.md" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
@pytest.mark.parametrize("mode", ["--check", "--assemble"])
def test_an_unreadable_fragment_directory_is_an_io_error_not_a_violation(tmp_path, mode):
    root = _repo(tmp_path)
    _frag(root, "a.md", GOOD)
    directory = root / "changelog.d"
    directory.chmod(0)
    try:
        assert _run(root, mode) == 2
    finally:
        directory.chmod(stat.S_IRWXU)
    assert _run(root, "--check") == 0  # near-miss: readable again, same fixture, green


@pytest.mark.parametrize("section", ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"])
def test_every_section_is_green(tmp_path, section):
    root = _repo(tmp_path)
    _frag(root, "ok.md", f"### {section}\n\n- **Title.** Text.\n")
    assert _run(root, "--check") == 0


def test_near_miss_fragments_are_green(tmp_path):
    """Look like violations but are legitimate: markers and hashes inside text or code, continuation lines, several
    bullets, CRLF line endings, no final newline, README.md left alone."""
    root = _repo(tmp_path)
    _frag(root, "a-multi.md", "### Added\n\n- **One.** First.\n  continued line.\n\n- **Two.** Second.\n")
    _frag(root, "b-inline.md", "### Changed\n\n- **Markers.** Text with <<<<<<< and >>>>>>> and ======= inside a line, "
                              "and a ====== six-sign line below.\n  ======\n")
    _frag(root, "c-fence.md", "### Fixed\n\n- **Code.** Example:\n\n  ```\n# not a heading\n  ```\n")
    _frag(root, "c2-indented.md", "### Fixed\n\n- **Code.** Example:\n\n  ```\n  ### Added\n  [adr]: x\n  ```\n"
                                  "  [a link]: inside a bullet is indented text, not a definition\n")
    _frag(root, "c3-inline-link.md", "### Added\n\n- **Links.** See [the ADR](docs/x.md), [ref][adr] and a "
                                     "bracket [not a def]: mid-line.\n")
    _frag(root, "d-crlf.md", "### Security\r\n\r\n- **Windows.** Text.\r\n")
    _frag(root, "e-nonl.md", "### Removed\n\n- **No final newline.** Text.")
    _frag(root, "f-unicode.md", "### Deprecated\n\n- **Unicode — dash.** Text.\n")
    assert _run(root, "--check") == 0


def test_no_fragment_directory_is_green(tmp_path):
    root = tmp_path / "repo"
    _write(root / "CHANGELOG.md", BASE_LOG)
    assert _run(root, "--check") == 0


# ------------------------------------------------------------------------------------------ CHANGELOG.md structure
@pytest.mark.parametrize("mutate, needle", [
    (lambda t: t.replace("## [Unreleased]\n", ""), "exactly one"),
    (lambda t: t.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [Unreleased]\n"), "exactly one"),
    (lambda t: t.replace("[unreleased]: https://example.invalid/compare/v0.1.0...HEAD\n", ""), "compare link"),
    (lambda t: t.replace("### Fixed\n\n- **Old fixed.** Already there.\n\n", "### Fixed\n\n- **Old fixed.** x.\n\n"
                         "### Added\n\n- **Again.** x.\n\n"), "appears 2 times"),
    (lambda t: t.replace("### Added\n\n- **Old added.** Already there.\n\n### Fixed", "### Fixed\n\n### Added"),
     "out of Keep-a-Changelog order"),
    (lambda t: t.replace("### Fixed", "### Improved", 1), "unknown section"),
    (lambda t: t.replace("## [Unreleased]\n", "<<<<<<< HEAD\n## [Unreleased]\n=======\n>>>>>>> x\n"), "conflict marker"),
    (lambda t: t.replace("# Changelog", "# Log", 1), "# Changelog"),
    (lambda t: t.replace("## [Unreleased]", "## [0.0.9] - 2025-01-01\n\n## [Unreleased]"), "release heading"),
])
def test_changelog_structure_violation_is_red(tmp_path, capsys, mutate, needle):
    mutated = mutate(BASE_LOG)
    assert mutated != BASE_LOG
    root = _repo(tmp_path, mutated)
    assert _run(root, "--check") == 1
    assert needle in capsys.readouterr().out


def test_changelog_structure_near_misses_are_green(tmp_path):
    empty_unreleased = BASE_LOG.replace(BASE_LOG[BASE_LOG.index("### Added"):BASE_LOG.index("## [0.1.0]")], "")
    only_security = empty_unreleased.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Security\n\n- **S.** x.\n")
    crlf = BASE_LOG.replace("\n", "\r\n")
    for log in (BASE_LOG, empty_unreleased, only_security, crlf):
        assert cl.structure_problems(log) == [], log
    assert cl.structure_problems(BASE_LOG.replace("[unreleased]:", "[Unreleased]:")) == []  # link label is case-free


def test_check_reports_a_missing_changelog_as_an_io_error(tmp_path):
    assert _run(tmp_path, "--check") == 2


# ------------------------------------------------------------------------------------------ assemble
def test_assemble_moves_fragments_in_order_and_creates_sections_in_keep_a_changelog_order(tmp_path):
    root = _repo(tmp_path)
    _frag(root, "02-fixed.md", "### Fixed\n\n- **Fixed two.** B.\n")
    _frag(root, "01-fixed.md", "### Fixed\n\n- **Fixed one.** A.\n\n- **Fixed one b.** A2.\n")
    _frag(root, "03-security.md", "### Security\n\n- **Sec.** S.\n")
    _frag(root, "04-changed.md", "### Changed\n\n- **Chg.** C.\n")
    _frag(root, "05-removed.md", "### Removed\n\n- **Rem.** R.\n")
    assert _run(root, "--assemble") == 0
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = log[log.index("## [Unreleased]"):log.index("## [0.1.0]")]
    heads = [line for line in unreleased.splitlines() if line.startswith("### ")]
    assert heads == ["### Added", "### Changed", "### Removed", "### Fixed", "### Security"]
    assert unreleased.index("Old fixed") < unreleased.index("Fixed one.") < unreleased.index("Fixed one b.") \
        < unreleased.index("Fixed two.")
    assert "- **Old added.** Already there." in unreleased
    assert log.count("## [Unreleased]") == 1 and "## [0.1.0] - 2026-01-01" in log
    assert log.endswith("[0.1.0]: https://example.invalid/releases/tag/v0.1.0\n")
    assert sorted(p.name for p in (root / "changelog.d").iterdir()) == ["README.md"]
    assert cl.structure_problems(log) == []


def test_assemble_into_an_empty_unreleased_and_between_existing_sections(tmp_path):
    empty = BASE_LOG.replace(BASE_LOG[BASE_LOG.index("### Added"):BASE_LOG.index("## [0.1.0]")], "")
    root = _repo(tmp_path, empty)
    _frag(root, "x.md", "### Fixed\n\n- **F.** f.\n")
    _frag(root, "y.md", "### Added\n\n- **A.** a.\n")
    assert _run(root, "--assemble") == 0
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert log.index("### Added") < log.index("### Fixed") < log.index("## [0.1.0]")
    assert "## [Unreleased]\n\n### Added\n\n- **A.** a.\n\n### Fixed\n\n- **F.** f.\n\n## [0.1.0]" in log
    assert cl.structure_problems(log) == []


def test_assemble_is_idempotent(tmp_path):
    root = _repo(tmp_path)
    _frag(root, "a.md", GOOD)
    _frag(root, "b.md", "### Added\n\n- **B.** b.\n")
    assert _run(root, "--assemble") == 0
    first = (root / "CHANGELOG.md").read_bytes()
    assert _run(root, "--assemble") == 0
    assert (root / "CHANGELOG.md").read_bytes() == first
    # and with no fragments at all the file is byte for byte unchanged
    plain = _repo(tmp_path / "plain")
    before = (plain / "CHANGELOG.md").read_bytes()
    assert _run(plain, "--assemble") == 0
    assert (plain / "CHANGELOG.md").read_bytes() == before


def test_a_fenced_heading_cannot_corrupt_the_changelog_and_a_link_definition_is_refused(tmp_path):
    """The two ways a fragment that passed --check used to break the next --assemble."""
    root = _repo(tmp_path)
    _frag(root, "a.md", "### Added\n\n- **Linked.** See [the ADR][adr].\n\n[adr]: docs/decisions/x.md\n")
    assert _run(root, "--check") == 1
    (root / "changelog.d" / "a.md").unlink()
    # a link definition that already is in [Unreleased] (hand-edited) does not hide the sections after it
    log = BASE_LOG.replace("### Fixed\n", "[adr]: docs/x.md\n\n### Fixed\n", 1)
    (root / "CHANGELOG.md").write_bytes(log.encode("utf-8"))
    _frag(root, "b.md", "### Security\n\n- **Sec.** S.\n")
    _frag(root, "c.md", "### Changed\n\n- **Chg.** C.\n")
    assert _run(root, "--assemble") == 0
    assert _run(root, "--check") == 0
    _frag(root, "d.md", "### Security\n\n- **Sec two.** S2.\n")
    _frag(root, "e.md", "### Added\n\n- **Add two.** A2.\n")
    assert _run(root, "--assemble") == 0
    assert _run(root, "--check") == 0
    final = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert cl.structure_problems(final) == []
    assert final.count("### Security") == 1 and final.count("### Changed") == 1


def test_assemble_keeps_the_file_permissions(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits do not apply on Windows")
    root = _repo(tmp_path)
    _frag(root, "a.md", GOOD)
    path = root / "CHANGELOG.md"
    path.chmod(0o640)  # neither the 0600 of mkstemp nor the usual 0644
    assert _run(root, "--assemble") == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_assemble_preserves_crlf_changelogs(tmp_path):
    root = _repo(tmp_path, BASE_LOG.replace("\n", "\r\n"))
    _frag(root, "a.md", GOOD)
    assert _run(root, "--assemble") == 0
    raw = (root / "CHANGELOG.md").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"- **Title.** What changed" in raw


def test_dry_run_changes_nothing(tmp_path, capsys):
    root = _repo(tmp_path)
    _frag(root, "a.md", GOOD)
    before = (root / "CHANGELOG.md").read_bytes()
    assert _run(root, "--assemble", "--dry-run") == 0
    assert (root / "CHANGELOG.md").read_bytes() == before and (root / "changelog.d" / "a.md").is_file()
    assert "WOULD MOVE" in capsys.readouterr().out


def test_assemble_refuses_and_changes_nothing_when_a_fragment_is_malformed(tmp_path, capsys):
    root = _repo(tmp_path)
    _frag(root, "a.md", GOOD)
    _frag(root, "b.md", "- **No heading.** x.\n")
    before = (root / "CHANGELOG.md").read_bytes()
    assert _run(root, "--assemble") == 1
    assert (root / "CHANGELOG.md").read_bytes() == before
    assert sorted(p.name for p in (root / "changelog.d").iterdir()) == ["README.md", "a.md", "b.md"]
    assert "b.md" in capsys.readouterr().out


def test_assemble_refuses_a_changelog_with_a_broken_structure(tmp_path):
    root = _repo(tmp_path, BASE_LOG.replace("## [Unreleased]\n", ""))
    _frag(root, "a.md", GOOD)
    assert _run(root, "--assemble") == 1
    assert (root / "changelog.d" / "a.md").is_file()


def test_assembled_real_changelog_passes_the_structure_test(tmp_path):
    """The real CHANGELOG.md plus fragments in every section, assembled, still passes the repository's own
    CHANGELOG structure test (tests/unit/test_repository_hygiene.py), and a second assemble changes nothing."""
    root = tmp_path / "repo"
    shutil.copy(ROOT / "CHANGELOG.md", _write(root / "CHANGELOG.md", ""))
    for i, section in enumerate(cl.SECTIONS):
        _frag(root, f"{i}-{section.lower()}.md", f"### {section}\n\n- **Bite {section}.** Text.\n")
    assert _run(root, "--assemble") == 0
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert_changelog_structure(log)
    for section in cl.SECTIONS:
        assert f"- **Bite {section}.** Text." in log
    assert cl.structure_problems(log) == []
    before = (root / "CHANGELOG.md").read_bytes()
    assert _run(root, "--assemble") == 0
    assert (root / "CHANGELOG.md").read_bytes() == before
    # the released part of the file is untouched
    real = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert log.endswith(real[real.index("## [0.1.0]"):])


def test_real_repository_passes_check():
    assert cl.main(["--root", str(ROOT), "--check"]) == 0


# ------------------------------------------------------------------------------------------ CLI contract
def _cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "changelog.py"), *args], capture_output=True,
                          check=False, timeout=60)


def test_cli_exit_codes_and_ascii_output(tmp_path):
    ok = _repo(tmp_path / "ok")
    _frag(ok, "a.md", GOOD)
    bad = _repo(tmp_path / "bad")
    _frag(bad, "café.md", "no heading\n")
    done = _cli("--root", str(ok), "--check")
    assert done.returncode == 0
    failed = _cli("--root", str(bad), "--check")
    assert failed.returncode == 1
    for result in (done, failed):
        result.stdout.decode("ascii")
        result.stderr.decode("ascii")
    assert _cli().returncode == 2  # nothing asked
    assert _cli("--check", "--assemble").returncode == 2
    assert _cli("--root", str(ok), "--check", "--dry-run").returncode == 2
    assert _cli("--root", str(tmp_path / "missing"), "--check").returncode == 2


# ------------------------------------------------------------------------------------------ git: the point of it all
def _git_env(tmp_path: Path) -> dict[str, str]:
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    return env


def _git(root: Path, env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=False, timeout=60)
    if check:
        assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


@pytest.fixture
def git_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    env = _git_env(tmp_path)
    root = _repo(tmp_path)
    _git(root, env, "init", "-q")
    _git(root, env, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(root, env, "config", key, value)
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", "base")
    return root, env


def _branch_commit(root: Path, env: dict[str, str], branch: str, edit) -> None:
    _git(root, env, "checkout", "-q", "-b", branch, "main")
    edit()
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", f"change on {branch}")


def test_two_branches_with_different_fragments_merge_without_conflict(git_repo):
    root, env = git_repo
    _branch_commit(root, env, "a", lambda: _frag(root, "alpha.md", "### Added\n\n- **Alpha.** a.\n"))
    _branch_commit(root, env, "b", lambda: _frag(root, "beta.md", "### Added\n\n- **Beta.** b.\n"))
    _git(root, env, "checkout", "-q", "main")
    _git(root, env, "merge", "-q", "--no-edit", "a")
    merged = _git(root, env, "merge", "--no-edit", "b", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert (root / "changelog.d" / "alpha.md").is_file() and (root / "changelog.d" / "beta.md").is_file()
    assert _run(root, "--check") == 0
    assert _run(root, "--assemble") == 0
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert log.index("Old added") < log.index("Alpha.") < log.index("Beta.") < log.index("### Fixed")


def test_negative_control_the_same_two_edits_to_changelog_unreleased_do_conflict(git_repo, capsys):
    root, env = git_repo

    def add_bullet(text: str):
        def edit() -> None:
            path = root / "CHANGELOG.md"
            path.write_bytes(path.read_bytes().replace(b"### Added\n\n", f"### Added\n\n{text}\n\n".encode(), 1))
        return edit

    _branch_commit(root, env, "a", add_bullet("- **Alpha.** a."))
    _branch_commit(root, env, "b", add_bullet("- **Beta.** b."))
    _git(root, env, "checkout", "-q", "main")
    _git(root, env, "merge", "-q", "--no-edit", "a")
    merged = _git(root, env, "merge", "--no-edit", "b", check=False)
    assert merged.returncode != 0, "the control must conflict, otherwise the fragment test above proves nothing"
    assert "<<<<<<<" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert _run(root, "--check") == 1  # and the checker also catches a conflict left in CHANGELOG.md
    assert "conflict marker" in capsys.readouterr().out
    _git(root, env, "merge", "--abort")


# ------------------------------------------------------------------------------------------ CI wiring
CI_JOB_NAME = "browser e2e + a11y smoke [G3]"
CHECK_COMMAND = "scripts/changelog.py --check"


def ci_wiring_problems(ci: dict) -> list[str]:
    """Why a parsed ci.yml does not run the changelog check in the expected cheap ubuntu job (empty = wired)."""
    jobs = ci.get("jobs", {})
    hits = [(jid, job) for jid, job in jobs.items()
            if any(CHECK_COMMAND in (s.get("run") or "") for s in job.get("steps", []))]
    if len(hits) != 1:
        return [f"the check must run in exactly one job (found {len(hits)})"]
    _, job = hits[0]
    problems: list[str] = []
    if job.get("runs-on") != "ubuntu-latest":
        problems.append(f"the job must run on ubuntu-latest (found {job.get('runs-on')!r})")
    if job.get("name") != CI_JOB_NAME:
        problems.append(f"the job must keep its name {CI_JOB_NAME!r} (found {job.get('name')!r})")
    if "continue-on-error" in job:
        problems.append("the job must not set continue-on-error")
    steps = job["steps"]
    check = next(i for i, s in enumerate(steps) if CHECK_COMMAND in (s.get("run") or ""))
    if "continue-on-error" in steps[check]:
        problems.append("the check step must not set continue-on-error")
    setup = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/setup-python@")]
    if not setup or setup[0] > check:
        problems.append("python must be set up before the check runs")
    return problems


def _real_ci() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _mutated(mutate) -> dict:
    ci = copy.deepcopy(_real_ci())
    mutate(ci["jobs"]["browser-e2e"], ci)
    return ci


def _check_index(job: dict) -> int:
    return next(i for i, s in enumerate(job["steps"]) if CHECK_COMMAND in (s.get("run") or ""))


def _move_before_setup(job, ci):
    step = job["steps"].pop(_check_index(job))
    job["steps"].insert(0, step)


def _step_coe(job, ci):
    job["steps"][_check_index(job)]["continue-on-error"] = True


def _job_coe(job, ci):
    job["continue-on-error"] = True


def _drop_step(job, ci):
    job["steps"].pop(_check_index(job))


def _windows(job, ci):
    job["runs-on"] = "windows-latest"


def _rename(job, ci):
    job["name"] = "browser e2e"


def _also_elsewhere(job, ci):
    ci["jobs"]["frontend"]["steps"].append({"run": "python scripts/changelog.py --check"})


def _move_to_other_job(job, ci):
    step = job["steps"].pop(_check_index(job))
    ci["jobs"]["frontend"]["steps"].append(step)  # an ubuntu job without setup-python


@pytest.mark.parametrize("mutate, needle", [
    (_drop_step, "exactly one job (found 0)"),
    (_also_elsewhere, "exactly one job (found 2)"),
    (_move_before_setup, "set up before"),
    (_step_coe, "check step must not set continue-on-error"),
    (_job_coe, "job must not set continue-on-error"),
    (_windows, "ubuntu-latest"),
    (_rename, "keep its name"),
    (_move_to_other_job, "set up before"),
])
def test_ci_wiring_violations_are_red(mutate, needle):
    problems = ci_wiring_problems(_mutated(mutate))
    assert any(needle in p for p in problems), problems


def test_ci_wiring_near_misses_are_green():
    def harmless(job, ci):
        job["steps"].insert(0, {"name": "extra", "run": "echo hi"})  # unrelated step before setup-python
        job["timeout-minutes"] = 30
        ci["jobs"]["frontend"]["steps"].append({"run": "python scripts/other.py --check"})
    assert ci_wiring_problems(_mutated(harmless)) == []


def test_ci_runs_the_changelog_check_in_the_expected_ubuntu_job():
    ci = _real_ci()
    assert ci_wiring_problems(ci) == []
    assert ci["jobs"]["browser-e2e"]["name"] == CI_JOB_NAME  # the required check keeps its name
