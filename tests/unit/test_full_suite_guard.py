"""The full-suite record (scripts/full_suite_record.py, written by scripts/run_tests.sh), the full-suite guard of the
pre-push hook and the conflict-markers gate of scripts/gates.py.

LESSONS L8 (strike 3, 2026-10-27): a branch tested only with targeted tests was pushed and CI went red on a test the
full suite runs. LESSONS L22 (strike 2, 2026-10-27): a scripted conflict resolution failed, the next step was chained
with ';' and committed the conflict markers. Every check has a BITE TEST: a violating fixture is red with its own
message and a near-miss legitimate fixture is green. Hermetic: throwaway repositories under tmp_path, no global or
system git config, placeholder identities, the real suite is never run (a stand-in interpreter answers for pytest)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
ZERO = "0" * 40
ME = "dev@example.com"
NEEDS_BASH = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs bash; on Windows (where bash may be the WSL launcher) this is NOT-PROVEN")


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aeos_script_{name}_fullsuite", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gates = _load("gates")
fsr = _load("full_suite_record")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Dev")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, ME)
    monkeypatch.delenv(gates.PUSH_WITHOUT_FULL_SUITE_ENV, raising=False)
    monkeypatch.setattr(gates, "run_facts", lambda: [])


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=repo,
                          env=gates.clean_env(), capture_output=True, text=True, encoding="utf-8", check=True)
    return proc.stdout.strip()


def _write_commit(repo: Path, name: str, text: str, subject: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Dev")
    _git(path, "config", "user.email", ME)
    _write_commit(path, "README.md", "root\n", "feat: root")
    return path


def _tree(repo: Path, rev: str = "HEAD") -> str:
    return _git(repo, "rev-parse", f"{rev}^{{tree}}")


def _record_clean_full_run(repo: Path) -> str:
    """Record HEAD as run_tests.sh would after a passing full run on a clean tree; returns the printed line."""
    start = fsr.read_state(repo)
    assert start is not None and start.clean
    return fsr.record(repo, start.line, [], now=dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.UTC), load="1.00,2.00,3.00")


# ------------------------------------------------------------------ what counts as a full run
@pytest.mark.parametrize("args", [[], ["-q"], ["-x", "-v"], ["-n", "4"], ["-n4"], ["--numprocesses=2"], ["--tb=short"],
                                  ["-rA"], ["--durations", "10"], ["--durations=5"], ["--color=no"]])
def test_output_and_speed_options_keep_a_run_full(args: list[str]):
    assert fsr.partial_reason(args) is None


@pytest.mark.parametrize("args", [["tests/unit"], ["-k", "gates"], ["-m", "not slow"], ["--lf"], ["-p", "no:xdist"],
                                  ["--deselect", "tests/unit/test_a.py::test_b"], ["-q", "tests/unit/test_a.py"]])
def test_a_path_or_a_selection_makes_a_run_partial(args: list[str]):
    reason = fsr.partial_reason(args)
    assert reason is not None and reason.startswith("partial run (the pytest argument "), args


# ------------------------------------------------------------------ the recorder
def test_a_full_clean_passing_run_is_recorded_in_the_common_git_dir(repo: Path):
    line = _record_clean_full_run(repo)
    assert line.startswith("run_tests: full-suite record: recorded tree "), line
    stored = (repo / ".git" / fsr.RECORD_FILE).read_text(encoding="utf-8").splitlines()
    assert stored == [f"{_tree(repo)} {_git(repo, 'rev-parse', 'HEAD')} 2026-10-27T09:00:00Z 1.00,2.00,3.00"]


def test_bite_a_selection_in_pytest_addopts_is_a_partial_run(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """pytest reads PYTEST_ADDOPTS itself: a path or -k there would make a partial run look full (L8 tool bypass)."""
    start = fsr.read_state(repo)
    assert start is not None
    monkeypatch.setenv("PYTEST_ADDOPTS", "tests/unit/test_gates.py")
    line = fsr.record(repo, start.line, [])
    assert line == ("run_tests: full-suite record: not recorded: partial run (PYTEST_ADDOPTS is set and may select "
                    "or deselect tests)")
    assert not (repo / ".git" / fsr.RECORD_FILE).exists()
    monkeypatch.setenv("PYTEST_ADDOPTS", "   ")  # near miss: blank is no selection
    assert fsr.decide(start, start, [], {"PYTEST_ADDOPTS": "   "}) is None


def test_a_partial_run_is_not_recorded_and_says_why(repo: Path):
    start = fsr.read_state(repo)
    assert start is not None
    line = fsr.record(repo, start.line, ["-k", "gates"])
    assert line == ("run_tests: full-suite record: not recorded: partial run (the pytest argument '-k' may select "
                    "or deselect tests)")
    assert not (repo / ".git" / fsr.RECORD_FILE).exists()


def test_a_dirty_tree_is_not_recorded_at_start_or_after_a_change_during_the_run(repo: Path):
    (repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    dirty = fsr.read_state(repo)
    assert dirty is not None and not dirty.clean
    assert "not recorded: the working tree was not clean when the run started" in fsr.record(repo, dirty.line, [])
    (repo / "untracked.py").unlink()
    start = fsr.read_state(repo)
    assert start is not None and start.clean
    (repo / "README.md").write_text("edited during the run\n", encoding="utf-8")
    assert "not recorded: the working tree changed during the run" in fsr.record(repo, start.line, [])
    _git(repo, "checkout", "--", "README.md")
    _write_commit(repo, "b.txt", "b\n", "feat: b")  # HEAD moves while the suite runs
    assert "not recorded: HEAD moved during the run" in fsr.record(repo, start.line, [])
    assert "could not be read" in fsr.record(repo, fsr.UNKNOWN, [])
    assert not (repo / ".git" / fsr.RECORD_FILE).exists()


def test_the_recorder_cli_never_fails_and_prints_one_line(repo: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]):
    monkeypatch.chdir(repo)
    assert fsr.main(["state"]) == 0
    state = capsys.readouterr().out.strip()
    assert fsr.parse_state(state) == fsr.read_state(repo)
    assert fsr.main(["record", "--start", state, "--", "-k", "x"]) == 0
    assert "not recorded: partial run" in capsys.readouterr().out

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(fsr, "record", boom)
    assert fsr.main(["record", "--start", state]) == 0
    assert capsys.readouterr().out.strip() == "run_tests: full-suite record: not recorded: the recorder failed: disk on fire"


# ------------------------------------------------------------------ the full-suite guard
def test_no_record_is_a_fail_with_the_exact_message(repo: Path):
    sha = _git(repo, "rev-parse", "HEAD")
    result = gates.full_suite_guard(sha, repo, gates.clean_env())
    assert result.status == gates.FAIL
    assert result.detail == (f"full suite not run for tree {_tree(repo)[:12]} ({sha}): run scripts/run_tests.sh on a "
                             "clean tree, or push with AEOS_PUSH_WITHOUT_FULL_SUITE='<reason>'")


def test_a_record_for_another_tree_is_a_fail_and_one_for_this_tree_a_pass(repo: Path):
    _record_clean_full_run(repo)  # the record is for the root commit's tree
    sha = _write_commit(repo, "b.txt", "b\n", "feat: b")
    other = gates.full_suite_guard(sha, repo, gates.clean_env())
    assert other.status == gates.FAIL and f"full suite not run for tree {_tree(repo)[:12]} ({sha})" in other.detail
    _record_clean_full_run(repo)
    assert gates.full_suite_guard(sha, repo, gates.clean_env()).status == gates.PASS


def test_the_same_tree_under_another_commit_passes_content_addressed(repo: Path):
    _record_clean_full_run(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: same tree, new commit")
    assert gates.full_suite_guard(_git(repo, "rev-parse", "HEAD"), repo, gates.clean_env()).status == gates.PASS


def test_a_record_written_in_a_linked_worktree_counts_in_the_main_checkout(repo: Path, tmp_path: Path):
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "topic", str(linked))
    sha = _write_commit(linked, "c.txt", "c\n", "feat: c")
    assert gates.full_suite_guard(sha, repo, gates.clean_env()).status == gates.FAIL
    _record_clean_full_run(linked)
    assert gates.full_suite_guard(sha, repo, gates.clean_env()).status == gates.PASS
    assert not (linked / ".git").is_dir()  # the record lives in the shared common dir, not per worktree


def test_a_bypass_reason_is_skipped_never_pass_and_a_blank_one_is_no_bypass(repo: Path):
    sha = _git(repo, "rev-parse", "HEAD")
    skipped = gates.full_suite_guard(sha, repo, gates.clean_env(), "docs only, CI runs the suite")
    assert skipped.status == gates.SKIPPED
    assert skipped.detail.startswith("AEOS_PUSH_WITHOUT_FULL_SUITE is set: docs only, CI runs the suite")
    assert gates.full_suite_guard(sha, repo, gates.clean_env(), "   ").status == gates.FAIL


def _hook_without_slow_gates(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """run_hook with every per-target gate replaced by a recorder: only the guards run for real."""
    ran: list[str] = []
    monkeypatch.setattr(gates, "merge_conflict_result", lambda *a, **k: ran.append("merge") or gates.Result(
        "merge-conflict", gates.PASS))
    monkeypatch.setattr(gates, "conflict_markers_result", lambda *a, **k: gates.Result("conflict-markers", gates.PASS))
    monkeypatch.setattr(gates, "STATIC_GATES", ())
    monkeypatch.setattr(gates, "FRONTEND_GATES", ())
    for fn in ("shellcheck_result", "actionlint_result", "actionlint_control_result"):
        monkeypatch.setattr(gates, fn, lambda *a, **k: gates.Result("stand-in", gates.SKIPPED, 0.0, "stand-in"))
    monkeypatch.setattr(gates, "commit_subject_gate", lambda base, head: gates.Gate("commit-subjects",
                                                                                   ("python", "-c", "pass")))
    return ran


def test_the_hook_refuses_a_push_without_a_record_before_any_gate_and_takes_the_env_reason(
        repo: Path, monkeypatch: pytest.MonkeyPatch):
    ran = _hook_without_slow_gates(monkeypatch)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    line = f"refs/heads/main {sha} refs/heads/main {ZERO}\n"
    refused = {r.gate: r for r in gates.run_hook(line, repo)}
    assert refused["full-suite"].status == gates.FAIL and "full suite not run for tree" in refused["full-suite"].detail
    assert ran == []  # a refused push spends nothing on the gates
    monkeypatch.setenv(gates.PUSH_WITHOUT_FULL_SUITE_ENV, "hotfix, owner said so")
    bypassed = {r.gate: r for r in gates.run_hook(line, repo)}
    assert bypassed["full-suite"].status == gates.SKIPPED and "hotfix, owner said so" in bypassed["full-suite"].detail
    assert ran == ["merge"]
    monkeypatch.delenv(gates.PUSH_WITHOUT_FULL_SUITE_ENV)
    _record_clean_full_run(repo)
    assert {r.gate: r for r in gates.run_hook(line, repo)}["full-suite"].status == gates.PASS


def test_a_deletion_push_is_not_checked_for_a_record(repo: Path):
    assert gates.run_hook(f"(delete) {ZERO} refs/heads/gone {'b' * 40}\n", repo) == []


def test_a_local_run_prints_whether_head_has_a_record(repo: Path):
    assert gates.full_suite_note(repo).startswith(
        f"full suite: no passing record for HEAD tree {_tree(repo)[:12]}; the pre-push hook refuses to push it")
    _record_clean_full_run(repo)
    note = gates.full_suite_note(repo)
    assert note.startswith(f"full suite: HEAD tree {_tree(repo)[:12]} has a passing record (2026-10-27T09:00:00Z")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    assert "uncommitted changes" in gates.full_suite_note(repo)


# ------------------------------------------------------------------ scripts/run_tests.sh records through the helper
STUB = """#!/bin/sh
# Stand-in interpreter: the recorder runs for real, pytest only says it passed.
case "$1" in
  *full_suite_record.py) exec "$STUB_REAL_PYTHON" "$@" ;;
esac
case "$*" in
  "-m pip check"|"-c import pytest"|"-c import xdist") exit 0 ;;
  "-m pytest"*) echo "pytest ran"; exit "${STUB_PYTEST_RC:-0}" ;;
esac
exit 99
"""


@pytest.fixture()
def project(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway checkout carrying the real run_tests.sh and recorder, and a stand-in python."""
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    for name in ("run_tests.sh", "full_suite_record.py"):
        shutil.copy(ROOT / "scripts" / name, proj / "scripts" / name)
    _git(proj, "init", "-q")
    _git(proj, "add", ".")
    _git(proj, "commit", "-q", "-m", "feat: scripts")
    stub = tmp_path / "stub bin" / "python"
    stub.parent.mkdir()
    stub.write_text(STUB, encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return proj, stub


def _run_tests(project: tuple[Path, Path], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    proj, stub = project
    env = gates.clean_env()
    env.update(AEOS_GATES_PYTHON=str(stub), STUB_REAL_PYTHON=sys.executable, **extra)
    env.pop("AEOS_TEST_DEADLINE_MIN", None)
    return subprocess.run(["bash", str(proj / "scripts" / "run_tests.sh"), *args], cwd=proj, env=env,
                          capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)


def _records(proj: Path) -> list[str]:
    path = proj / ".git" / fsr.RECORD_FILE
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@NEEDS_BASH
def test_run_tests_records_a_full_clean_pass_and_nothing_else(project: tuple[Path, Path]):
    proj, _ = project
    partial = _run_tests(project, "--", "-k", "gates")
    assert partial.returncode == 0, partial.stderr
    assert "full-suite record: not recorded: partial run" in partial.stdout and _records(proj) == []
    assert partial.stdout.strip().splitlines()[-1] == "RUN_TESTS: PASS rc=0"
    failed = _run_tests(project, STUB_PYTEST_RC="1")
    assert "not recorded: the run did not pass (rc=1)" in failed.stdout and _records(proj) == []
    (proj / "new_test.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    dirty = _run_tests(project)
    assert "not recorded: the working tree was not clean" in dirty.stdout and _records(proj) == []
    (proj / "new_test.py").unlink()
    full = _run_tests(project, "--", "-x", "-n", "2")  # near miss: options that select nothing still count as full
    assert full.returncode == 0, full.stderr
    assert "full-suite record: recorded tree" in full.stdout
    assert full.stdout.strip().splitlines()[-1] == "RUN_TESTS: PASS rc=0"
    (record,) = _records(proj)
    tree, head, when, load = record.split()
    assert (tree, head) == (_tree(proj), _git(proj, "rev-parse", "HEAD")) and when.endswith("Z") and load


# ------------------------------------------------------------------ the conflict-markers gate (LESSONS L22)
@pytest.fixture()
def branch(repo: Path) -> Path:
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "topic")
    return repo


def test_a_committed_conflict_marker_fails_naming_file_and_line(branch: Path):
    _write_commit(branch, "src/b.txt", "x\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n", "fix: oops")
    result = gates.conflict_markers_result(branch, gates.clean_env())
    assert result.status == gates.FAIL
    assert "3 leftover conflict marker(s) committed" in result.detail
    for place in ("src/b.txt:2", "src/b.txt:4", "src/b.txt:6"):
        assert f"  {place}\n" in result.detail, result.detail


def test_a_lone_opening_marker_fails_too(branch: Path):
    _write_commit(branch, "c.py", "a = 1\n<<<<<<< ours\n", "fix: half a conflict")
    result = gates.conflict_markers_result(branch, gates.clean_env())
    assert result.status == gates.FAIL and "c.py:2" in result.detail


def test_a_setext_heading_underline_passes_although_git_flags_it(branch: Path):
    """Near miss: git diff --check reports a lone '=======' line as a leftover conflict marker; the gate does not."""
    _write_commit(branch, "docs/a.md", "Title\n=======\n\ntext with trailing space \n", "docs: heading")
    raw = subprocess.run(["git", "diff", "--check", "origin/main", "HEAD"], cwd=branch, env=gates.clean_env(),
                         capture_output=True, text=True, check=False)
    assert "docs/a.md:2: leftover conflict marker" in raw.stdout and "trailing whitespace" in raw.stdout
    result = gates.conflict_markers_result(branch, gates.clean_env())
    assert result.status == gates.PASS, result.detail


def test_the_conflict_markers_gate_is_skipped_without_origin_main(repo: Path):
    result = gates.conflict_markers_result(repo, gates.clean_env())
    assert result.status == gates.SKIPPED and "origin/main not found" in result.detail


def test_only_the_pushed_tips_range_is_checked_in_hook_mode(branch: Path):
    marked = _write_commit(branch, "m.txt", ">>>>>>> theirs\n", "fix: marker")
    _git(branch, "checkout", "-q", "main")
    clean = _write_commit(branch, "n.txt", "fine\n", "feat: clean")
    assert gates.conflict_markers_result(branch, gates.clean_env(), marked).status == gates.FAIL
    assert gates.conflict_markers_result(branch, gates.clean_env(), clean).status == gates.PASS
