"""Local gate runner, pre-push hook and commit-subject gate (scripts/gates.py, scripts/commit_subject.py).

Hermetic: no network, no push. Git is only used on throwaway repositories under tmp_path (identity set per
command, a placeholder address) and on the read-only index of this repository."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from aeos.core.paths import repo_root

ROOT = repo_root()
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aeos_script_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load("commit_subject")
gates = _load("gates")


# ------------------------------------------------------------------ throwaway repositories
def _git(repo: Path, *args: str) -> str:
    env = gates.clean_env()
    env.update(GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL="dev@example.com",
               GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@example.com")
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    return proc.stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    _git(repo, "commit", "--allow-empty", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    return path


# ------------------------------------------------------------------ the subject rule
@pytest.mark.parametrize("subject", [
    "feat: add a thing",
    "fix(core): keep inherited ACLs",
    "feat(dev)!: breaking change",
    "chore(deps-dev): bump x",
    "security(broker): refuse the push",
    "revert: feat(x): y",
])
def test_subject_accept_cases(subject: str):
    assert cs.check_subject(subject) is None


@pytest.mark.parametrize("subject", [
    "Update README",
    "feat add a thing",
    "feat:no space",
    "feat:  two spaces",
    "feat: ",
    "feat(): empty scope",
    "feat(a b): space in scope",
    "ci,docs: scope list",
    "wip: not a type",
    "Feat: capitalised type",
    "feat(core) fix: missing colon",
    "",
])
def test_subject_reject_cases(subject: str):
    assert cs.check_subject(subject) is not None


def test_first_line_is_limited_to_100_characters():
    prefix = "feat: "
    assert cs.check_subject(prefix + "x" * (100 - len(prefix))) is None
    reason = cs.check_subject(prefix + "x" * (101 - len(prefix)))
    assert reason is not None and "101" in reason


def test_every_type_of_the_rule_is_accepted():
    assert cs.TYPES == ("feat", "fix", "docs", "test", "ci", "build", "chore", "refactor", "perf", "style",
                        "revert", "security")
    for kind in cs.TYPES:
        assert cs.check_subject(f"{kind}(scope): description") is None


# ------------------------------------------------------------------ ranges, merges, baseline
def test_merge_commits_are_exempt_and_other_commits_are_checked(repo: Path):
    base = _commit(repo, "feat: root")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "fix: side work")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "fix: main work")
    _git(repo, "merge", "--no-ff", "-m", "Merge branch 'side' into main", "side")
    _commit(repo, "not conventional")
    found = cs.violations(f"{base}..HEAD", str(repo), baseline=None)
    assert [subject for _, subject, _ in found] == ["not conventional"]
    assert [s for _, s in cs.commits(f"{base}..HEAD", str(repo), baseline=None)] == [
        "not conventional", "fix: main work", "fix: side work"]


def test_commits_at_or_below_the_baseline_are_not_checked(repo: Path):
    _commit(repo, "old and sloppy")
    baseline = _commit(repo, "also sloppy")
    _commit(repo, "feat: new work")
    bad_new = _commit(repo, "sloppy again")
    assert [s for _, s, _ in cs.violations("HEAD", str(repo), baseline=None)] == [
        "sloppy again", "also sloppy", "old and sloppy"]
    found = cs.violations("HEAD", str(repo), baseline=baseline)
    assert [(sha, subject) for sha, subject, _ in found] == [(bad_new, "sloppy again")]


def test_an_unknown_baseline_fails_closed(repo: Path):
    _commit(repo, "feat: x")
    with pytest.raises(RuntimeError, match="baseline"):
        cs.commits("HEAD", str(repo), baseline="0123456789abcdef0123456789abcdef01234567")


def test_the_baseline_is_a_full_sha_with_a_dated_reason():
    assert re.fullmatch(r"[0-9a-f]{40}", cs.BASELINE)
    source = (ROOT / "scripts" / "commit_subject.py").read_text(encoding="utf-8")
    assert "2026-10-26" in source.split("BASELINE =", 1)[0].rsplit("\n\n", 1)[-1]


def test_cli_exit_codes_and_ascii_output(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    first = _commit(repo, "feat: root")
    _commit(repo, "feat: café → ok")
    _commit(repo, "résumé without a type")
    monkeypatch.chdir(repo)
    assert cs.main([f"{first}..HEAD", "--baseline", "none"]) == 1
    out = capsys.readouterr().out
    assert out.isascii() and "without a type" in out
    assert cs.main([f"{first}..HEAD~1", "--baseline", "none"]) == 0
    assert cs.main(["HEAD", "--baseline", "0" * 40]) == 2  # unknown baseline: error, never a silent pass
    assert cs.main(["no-such-ref..HEAD", "--baseline", "none"]) == 2


# ------------------------------------------------------------------ hook stdin
ZERO = "0" * 40


def test_push_lines_are_parsed_and_deletions_are_skipped(repo: Path):
    sha = _commit(repo, "feat: x")
    text = (f"refs/heads/topic {sha} refs/heads/topic {'a' * 40}\n"
            f"(delete) {ZERO} refs/heads/gone {'b' * 40}\n"
            "\n")
    refs = gates.parse_push_lines(text)
    assert [r.is_deletion for r in refs] == [False, True]
    assert refs[0] == gates.PushRef("refs/heads/topic", sha, "refs/heads/topic", "a" * 40)
    targets = gates.hook_targets(refs, repo, gates.clean_env())
    assert [r.local_ref for r, _ in targets] == ["refs/heads/topic"]


def test_a_new_branch_uses_origin_main_and_an_existing_one_its_remote_sha(repo: Path):
    old = _commit(repo, "feat: old")
    new = _commit(repo, "feat: new")
    env = gates.clean_env()
    _git(repo, "update-ref", "refs/remotes/origin/main", old)
    created = gates.PushRef("refs/heads/n", new, "refs/heads/n", ZERO)
    updated = gates.PushRef("refs/heads/u", new, "refs/heads/u", old)
    assert gates.hook_targets([created], repo, env) == [(created, "origin/main")]
    assert gates.hook_targets([updated], repo, env) == [(updated, old)]
    # a remote sha this clone does not have falls back to origin/main instead of failing the range
    unknown = gates.PushRef("refs/heads/u", new, "refs/heads/u", "c" * 40)
    assert gates.hook_targets([unknown], repo, env) == [(unknown, "origin/main")]
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert gates.hook_targets([created], repo, env) == [(created, None)]


def test_a_sha_pushed_to_two_refs_is_checked_once(repo: Path):
    sha = _commit(repo, "feat: x")
    refs = [gates.PushRef("refs/heads/a", sha, "refs/heads/a", ZERO), gates.PushRef("refs/heads/b", sha, "refs/heads/b", ZERO)]
    assert len(gates.hook_targets(refs, repo, gates.clean_env())) == 1


def test_a_hook_run_with_only_deletions_checks_nothing(repo: Path):
    assert gates.run_hook(f"(delete) {ZERO} refs/heads/gone {'b' * 40}\n", repo) == []


# ------------------------------------------------------------------ temporary worktree and environment
def test_the_temporary_worktree_is_outside_the_repository_link_free_and_removed(repo: Path):
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    sha = _commit(repo, "feat: file")
    with gates.detached_worktree(repo, sha, gates.clean_env()) as tmp:
        assert repo.resolve() not in (tmp, *tmp.parents)
        assert tmp.resolve() not in (repo.resolve(), *repo.resolve().parents)
        assert (tmp / "file.txt").read_text(encoding="utf-8") == "x\n"
        assert _git(tmp, "rev-parse", "HEAD") == sha
        assert not [p for p in tmp.rglob("*") if p.is_symlink()]
        assert len(_git(repo, "worktree", "list").splitlines()) == 2
    assert not tmp.exists()
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


def test_the_temporary_worktree_is_removed_when_a_gate_raises(repo: Path):
    sha = _commit(repo, "feat: x")
    with pytest.raises(ValueError), gates.detached_worktree(repo, sha, gates.clean_env()) as tmp:
        raise ValueError("boom")
    assert not tmp.exists() and len(_git(repo, "worktree", "list").splitlines()) == 1


def test_a_worktree_that_cannot_be_created_leaves_no_directory(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    made = tmp_path / "made"
    monkeypatch.setattr(gates.tempfile, "mkdtemp", lambda prefix: str(made.mkdir() or made))
    with pytest.raises(RuntimeError, match="worktree add"), gates.detached_worktree(repo, "f" * 40, gates.clean_env()):
        pass
    assert not made.exists()


def test_gates_never_create_a_link():
    """A junction inside a worktree once made `git worktree remove` delete half of a real venv."""
    tree = ast.parse((ROOT / "scripts" / "gates.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not {"symlink", "symlink_to", "link", "hardlink_to", "junction"} & names
    assert not [s for s in strings if "mklink" in s.lower() or "junction" in s.lower() and len(s) < 30]


def test_git_variables_are_scrubbed_from_every_gate_environment(monkeypatch: pytest.MonkeyPatch, repo: Path):
    for name in gates.SCRUBBED_ENV:
        monkeypatch.setenv(name, str(repo / "nowhere"))
    monkeypatch.setenv("AEOS_KEEP_ME", "1")
    env = gates.clean_env()
    assert set(gates.SCRUBBED_ENV) == {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
                                       "GIT_OBJECT_DIRECTORY"}
    assert not set(gates.SCRUBBED_ENV) & set(env) and env["AEOS_KEEP_ME"] == "1"
    probe = gates.Gate("probe", ("python", "-c", "import os, sys; sys.exit(1 if any(k.startswith('GIT_') and k in "
                                 f"{list(gates.SCRUBBED_ENV)!r} for k in os.environ) else 0)"))
    sha = _commit(repo, "feat: x")
    monkeypatch.setattr(gates, "STATIC_GATES", (probe,))
    monkeypatch.setattr(gates, "commit_subject_gate",
                        lambda base, head: gates.Gate("commit-subjects", ("python", str(gates.COMMIT_SUBJECT),
                                                                         f"{base}..{head}", "--baseline", "none")))
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    results = gates.run_hook(f"refs/heads/t {sha} refs/heads/t {ZERO}\n", repo)
    by_name = {r.gate: r for r in results}
    assert by_name["probe"].status == gates.PASS, by_name["probe"].detail  # GIT_DIR pointed nowhere, yet the run worked
    assert by_name["commit-subjects"].status == gates.PASS
    frontend = [r for r in results if r.gate.startswith("frontend")]
    assert [r.status for r in frontend] == [gates.SKIPPED, gates.SKIPPED]
    assert all("node_modules" in r.detail for r in frontend)


# ------------------------------------------------------------------ frontend gates are never a false PASS
def _frontend_repo(repo: Path, *, changed: bool) -> str:
    (repo / "frontend").mkdir()
    (repo / "frontend" / "a.ts").write_text("export {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    base = _commit(repo, "feat: frontend")
    if changed:
        (repo / "frontend" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    return base


def test_frontend_gates_skip_with_a_reason_when_nothing_changed_or_node_modules_is_missing(repo: Path, monkeypatch):
    base = _frontend_repo(repo, changed=False)
    ran = []
    monkeypatch.setattr(gates, "run_gate", lambda g, *a, **k: ran.append(g.name) or gates.Result(g.name, gates.PASS))
    env = gates.clean_env()
    (repo / "frontend" / "node_modules").mkdir()
    unchanged = gates.frontend_results(repo, base, False, env)
    assert [r.status for r in unchanged] == [gates.SKIPPED] * 2 and "no files under frontend/" in unchanged[0].detail
    (repo / "frontend" / "node_modules").rmdir()
    (repo / "frontend" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    missing = gates.frontend_results(repo, base, False, env)
    assert [r.status for r in missing] == [gates.SKIPPED] * 2 and "node_modules" in missing[0].detail
    unknown = gates.frontend_results(repo, None, False, env)
    assert "cannot tell" in unknown[0].detail
    assert ran == []  # nothing ran, so nothing may say PASS
    (repo / "frontend" / "node_modules").mkdir()
    assert [r.status for r in gates.frontend_results(repo, base, False, env)] == [gates.PASS] * 2
    assert ran == [g.name for g in gates.FRONTEND_GATES]


def test_untracked_files_under_frontend_count_as_a_change(repo: Path):
    base = _frontend_repo(repo, changed=False)
    (repo / "frontend" / "new.ts").write_text("export {}\n", encoding="utf-8")
    assert gates.frontend_changed(repo, base, gates.clean_env()) is True


# ------------------------------------------------------------------ runner behaviour
def test_run_gate_reports_pass_fail_and_skipped_honestly(tmp_path: Path):
    env = gates.clean_env()
    ok = gates.run_gate(gates.Gate("ok", ("python", "-c", "pass")), tmp_path, env)
    bad = gates.run_gate(gates.Gate("bad", ("python", "-c", "print('boom'); raise SystemExit(3)")), tmp_path, env)
    gone = gates.run_gate(gates.Gate("gone", ("no-such-tool-xyz", "--x")), tmp_path, env)
    assert (ok.status, bad.status, gone.status) == (gates.PASS, gates.FAIL, gates.SKIPPED)
    assert "exit 3" in bad.detail and "boom" in bad.detail and "not on PATH" in gone.detail


def test_exit_code_is_one_only_when_a_gate_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    results = [gates.Result("a", gates.PASS, 1.234), gates.Result("b", gates.SKIPPED, 0.0, "because")]
    monkeypatch.setattr(gates, "run_local", lambda args: results)
    assert gates.main([]) == 0
    table = capsys.readouterr().out
    assert "PASS" in table and "SKIPPED b: because" in table
    results.append(gates.Result("c", gates.FAIL, 2.0, "exit 1\nlast line", "python -m c"))
    assert gates.main(["--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and [r["status"] for r in data["results"]] == ["PASS", "SKIPPED", "FAIL"]
    assert set(data["results"][0]) == {"gate", "status", "seconds", "command", "detail", "ref"}


def test_output_is_ascii_only_even_for_non_ascii_tool_output(monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]):
    nasty = gates.Result("x", gates.FAIL, 0.5, "exit 1\ncafé → ✔", "python -c café", "ref@é")
    skipped = gates.Result("y", gates.SKIPPED, 0.0, "réason")
    monkeypatch.setattr(gates, "run_local", lambda args: [nasty, skipped])
    for flags in ([], ["--json"]):
        assert gates.main(flags) == 1
        out = capsys.readouterr().out
        assert out.isascii() and out.strip()
    assert gates.render_table([nasty, skipped]).isascii()
    assert gates.render_failures([nasty], hook=True).isascii()


def test_a_failing_hook_says_how_to_see_details_and_that_no_verify_is_not_this_tools_to_close():
    text = gates.render_failures([gates.Result("ruff", gates.FAIL, 1.0, "exit 1\nE501", "python -m ruff", "r@abc")], True)
    assert "python scripts/gates.py" in text and "--no-verify" in text and "E501" in text


def test_tests_run_in_parallel_only_when_xdist_is_importable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name: object())
    assert gates.test_gate().argv[-2:] == ("-n", "logical")
    monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name: None)
    assert "-n" not in gates.test_gate().argv and "pytest" in gates.test_gate().argv


# ------------------------------------------------------------------ drift guard
def _ci_commands(workflow_text: str, job_prefix: str, step_prefixes: tuple[str, ...]) -> list[str]:
    jobs = yaml.safe_load(workflow_text)["jobs"]
    job = next(j for jid, j in jobs.items() if jid.startswith(job_prefix))
    commands: list[str] = []
    for step in job["steps"]:
        if str(step.get("name", "")).startswith(step_prefixes):
            commands += [line.strip() for line in step["run"].splitlines() if line.strip()]
    return commands


def _drift(workflow_text: str) -> tuple[list[str], list[str]]:
    """(G1 commands in ci.yml, the static gates of scripts/gates.py), then the same for the frontend job."""
    return (_ci_commands(workflow_text, "backend-shards-", ("G1 static",)) + _ci_commands(
        workflow_text, "frontend", ("G1 static", "G3 product tests")),
        [g.command for g in gates.STATIC_GATES + gates.FRONTEND_GATES])


def test_gates_py_runs_exactly_the_g1_commands_of_ci_yml():
    in_ci, in_gates = _drift(CI_TEXT)
    assert in_ci == ["python -m ruff check backend tests scripts", "python -m mypy --platform darwin",
                     "python -m mypy --platform win32", "python scripts/secret_scan.py", "npx tsc -b --noEmit", "npm test"]
    assert in_ci == in_gates, "scripts/gates.py and the G1 steps of ci.yml diverged: change both together"


def test_every_shard_job_carries_the_same_g1_commands_the_guard_reads():
    jobs = yaml.safe_load(CI_TEXT)["jobs"]
    shard_ids = [jid for jid in jobs if jid.startswith("backend-shards-")]
    assert len(shard_ids) == 3
    reads = {tuple(_ci_commands(CI_TEXT, jid, ("G1 static",))) for jid in shard_ids}
    assert len(reads) == 1


@pytest.mark.parametrize("old, new", [
    ("python scripts/secret_scan.py", "python scripts/secret_scan.py --strict"),
    ("python -m ruff check backend tests scripts", "python -m ruff check backend tests"),
    ("python -m mypy --platform win32", "python -m mypy --platform linux"),
])
def test_the_drift_guard_catches_a_changed_ci_command(old: str, new: str):
    in_ci, in_gates = _drift(CI_TEXT.replace(old, new))
    assert in_ci != in_gates


def test_the_drift_guard_catches_a_removed_or_added_gate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gates, "STATIC_GATES", gates.STATIC_GATES[:-1])
    in_ci, in_gates = _drift(CI_TEXT)
    assert in_ci != in_gates
    monkeypatch.setattr(gates, "STATIC_GATES", (*gates.STATIC_GATES, gates.Gate("extra", ("python", "-c", "pass"))))
    assert _drift(CI_TEXT)[0] != _drift(CI_TEXT)[1]


# ------------------------------------------------------------------ wiring: workflow, hook file, docs
def test_the_commit_subject_job_runs_on_pull_requests_only_with_full_history():
    jobs = yaml.safe_load(CI_TEXT)["jobs"]
    job = jobs["commit-subjects"]
    assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == 5
    assert "pull_request" in job["if"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["fetch-depth"] == 0
    run = next(s for s in job["steps"] if "commit_subject.py" in str(s.get("run")))
    assert run["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert run["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'commit_subject.py "$BASE_SHA..$HEAD_SHA"' in run["run"]
    others = {jid: j for jid, j in jobs.items() if jid != "commit-subjects"}
    assert not [jid for jid, j in others.items() for s in j["steps"] if "fetch-depth" in (s.get("with") or {})]


def test_the_pre_push_hook_is_a_short_executable_posix_sh_file():
    path = ROOT / "scripts" / "git-hooks" / "pre-push"
    text = path.read_bytes().decode("utf-8")
    assert text.startswith("#!/bin/sh\n") and "\r" not in text and len(text.splitlines()) <= 20
    for needle in ("git rev-parse --show-toplevel", ".venv/bin/python", ".venv/Scripts/python.exe", "python3",
                   'exec "$py" "$root/scripts/gates.py" --hook "$@"'):
        assert needle in text, needle
    try:
        index = subprocess.run(["git", "ls-files", "-s", "scripts/git-hooks/pre-push"], cwd=ROOT, capture_output=True,
                               text=True, check=True, env=gates.clean_env()).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if index:
        assert index.startswith("100755 "), "mark it executable: git update-index --chmod=+x"


def test_contributing_documents_the_one_line_install_and_the_runner():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "git config core.hooksPath scripts/git-hooks" in text and "python scripts/gates.py" in text
