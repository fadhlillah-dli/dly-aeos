"""Local gate runner, pre-push hook and commit-subject gate (scripts/gates.py, scripts/commit_subject.py).

Hermetic: no network, no push. Git is only used on throwaway repositories under tmp_path (identity set per
command, a placeholder address) and on the read-only index of this repository."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from aeos.core.attribution import find_ai_attribution
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


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git config (hooksPath, signing, templates) reaches any git call of these tests, nor the
    scripts under test: an empty file stands in for the global one."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(gates, "run_facts", lambda: [])  # no preflight subprocess (it asks gh about its login)


# ------------------------------------------------------------------ throwaway repositories
def _git(repo: Path, *args: str, **identity: str) -> str:
    """Git on a throwaway repo. The identity comes from the environment, never from a global or system git config."""
    env = gates.clean_env()
    env.update(GIT_CONFIG_GLOBAL=os.environ.get("GIT_CONFIG_GLOBAL", os.devnull), GIT_CONFIG_NOSYSTEM="1")
    env.update(GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL="dev@example.com",
               GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@example.com")
    env.update({k.upper(): v for k, v in identity.items()})  # git_author_email="x" -> GIT_AUTHOR_EMAIL
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    return proc.stdout.strip()


def _commit(repo: Path, subject: str, **identity: str) -> str:
    _git(repo, "commit", "--allow-empty", "-m", subject, **identity)
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


# ------------------------------------------------------------------ --subject: a pull request title
# The title becomes the merge-commit body and the squash subject, so it gets the commit-subject rules (same functions,
# no copy). AI samples are assembled from parts so that the repository-wide hygiene scan finds nothing in this file.
TRAILER = "Co-authored-by"
AI_ADDR = "noreply" + "@" + "anthropic.com"


def _subject_main(subject: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cs.main(["--subject", subject])
    return code, capsys.readouterr().out


def test_a_valid_pull_request_title_passes(capsys: pytest.CaptureFixture[str]):
    code, out = _subject_main("ci: lint the workflows with actionlint", capsys)
    assert code == 0 and "1 subject checked" in out and "FAIL" not in out


def test_a_title_that_is_not_a_conventional_commits_subject_fails_with_the_reason(capsys: pytest.CaptureFixture[str]):
    code, out = _subject_main("wip", capsys)
    assert code == 1
    assert "FAIL 'wip'" in out and "expected 'type(scope)?!?: description' with type" in out and out.isascii()
    long_code, long_out = _subject_main("feat: " + "x" * 100, capsys)
    assert long_code == 1 and "characters (maximum 100)" in long_out


def test_an_attribution_in_a_pull_request_title_fails_inline_and_as_a_trailer(capsys: pytest.CaptureFixture[str]):
    trailer_in_body = f"feat: add a thing\n\n{TRAILER}: Some Bot <{AI_ADDR}>"
    inline = f"feat: add a thing {TRAILER}: Some Bot <{AI_ADDR}>"
    for title in (trailer_in_body, inline):
        code, out = _subject_main(title, capsys)
        assert code == 1 and "AI attribution in the message" in out, title
        assert AI_ADDR not in out and out.isascii()  # the guard's reasons never echo the address
    # the format passes and only the attribution is reported for the inline form
    assert cs.check_subject(inline) is None and cs.subject_reasons(inline)
    code, out = _subject_main("feat: the generated client is regenerated in CI", capsys)
    assert code == 0, out  # prose that mentions generation is not an attribution


@pytest.mark.parametrize("subject", ["feat: add a thing", "fix(core): keep inherited ACLs", "Update README", "feat: ",
                                     "Revert \"feat: x\"", "Revert \"not conventional\"", ""])
def test_the_title_check_gives_the_commit_subject_verdict(subject: str, capsys: pytest.CaptureFixture[str]):
    """One rule for commits and titles: --subject agrees with check_subject on every sample."""
    code, _ = _subject_main(subject, capsys)
    assert (code == 0) is (cs.check_subject(subject) is None)


def test_the_subject_mode_needs_exactly_one_of_a_range_or_a_subject(capsys: pytest.CaptureFixture[str]):
    for argv in ([], ["main..HEAD", "--subject", "feat: x"]):
        with pytest.raises(SystemExit) as raised:
            cs.main(argv)
        assert raised.value.code == 2
    capsys.readouterr()


def test_a_subject_that_starts_with_a_dash_is_taken_as_text_not_as_an_option():
    """The workflow passes --subject="$PR_TITLE": a title such as '-x' must fail the check, not break the usage."""
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "commit_subject.py"), "--subject=-x"],
                          capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    assert proc.returncode == 1 and "FAIL '-x'" in proc.stdout


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
    monkeypatch.setenv(gates.PUSH_WITHOUT_FULL_SUITE_ENV, "test: the full-suite guard has its own tests")
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
def _ci_commands(workflow_text: str, job_prefix: str, step_prefixes: tuple[str, ...],
                 skip: tuple[str, ...] = ()) -> list[str]:
    jobs = yaml.safe_load(workflow_text)["jobs"]
    job = next(j for jid, j in jobs.items() if jid.startswith(job_prefix))
    commands: list[str] = []
    for step in job["steps"]:
        name = str(step.get("name", ""))
        if name.startswith(step_prefixes) and not any(word in name for word in skip):
            commands += [line.strip() for line in step["run"].splitlines() if line.strip()]
    return commands


# The bite control of the actionlint step is a script of its own, mirrored by gates.actionlint_control_result and
# checked in tests/unit/test_ci_gates.py; the other G1 steps are one command each and must equal gates.py's, word for word.
BITE_CONTROL = ("bite control",)


def _drift(workflow_text: str) -> tuple[list[str], list[str]]:
    """(G1 commands in ci.yml, the static gates of scripts/gates.py), then the same for the frontend job."""
    return (_ci_commands(workflow_text, "static-checks", ("G1 static",), skip=BITE_CONTROL) + _ci_commands(
        workflow_text, "frontend", ("G1 static", "G3 product tests")),
        [g.command for g in gates.STATIC_GATES] + [gates.SHELLCHECK_COMMAND, gates.ACTIONLINT_COMMAND]
        + [g.command for g in gates.FRONTEND_GATES])


def test_gates_py_runs_exactly_the_g1_commands_of_ci_yml():
    in_ci, in_gates = _drift(CI_TEXT)
    assert in_ci == ["python -m ruff check backend tests scripts", "python -m mypy --platform darwin",
                     "python -m mypy --platform win32", "python scripts/secret_scan.py",
                     "shellcheck scripts/*.sh scripts/git-hooks/pre-push", "actionlint", "npx tsc -b --noEmit", "npm test"]
    assert in_ci == in_gates, "scripts/gates.py and the G1 steps of ci.yml diverged: change both together"


def test_the_guard_reads_the_one_static_job_and_no_shard_job_repeats_a_g1_command():
    """The G1 commands live in the `static-checks` job only (ubuntu-latest, once for all host OSes); a G1 step that
    came back into a shard job would run the check twice and the guard would not see it."""
    jobs = yaml.safe_load(CI_TEXT)["jobs"]
    assert [jid for jid in jobs if jid.startswith("static-checks")] == ["static-checks"]
    shard_ids = [jid for jid in jobs if jid.startswith("backend-shards-")]
    assert len(shard_ids) == 3
    for jid in shard_ids:
        assert _ci_commands(CI_TEXT, jid, ("G1",)) == [], jid
        assert not any("ruff" in (s.get("run") or "") or "mypy" in (s.get("run") or "") for s in jobs[jid]["steps"]), jid


@pytest.mark.parametrize("old, new", [
    ("python scripts/secret_scan.py", "python scripts/secret_scan.py --strict"),
    ("python -m ruff check backend tests scripts", "python -m ruff check backend tests"),
    ("python -m mypy --platform win32", "python -m mypy --platform linux"),
    ("shellcheck scripts/*.sh scripts/git-hooks/pre-push", "shellcheck scripts/*.sh"),
    ("run: actionlint\n", "run: actionlint -ignore 'property'\n"),
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
    assert text.startswith("#!/bin/sh\n") and "\r" not in text and len(text.splitlines()) <= 45
    for needle in ("git rev-parse --show-toplevel", "--git-common-dir", "AEOS_GATES_PYTHON", ".venv/bin/python",
                   ".venv/Scripts/python.exe", "python3", 'exec "$py" "$root/scripts/gates.py" --hook "$@"'):
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
    assert "AEOS_GATES_PYTHON" in text
    assert "git config core.hooksPath scripts/git-hooks" in text and "python scripts/gates.py" in text


# ------------------------------------------------------------------ revert subjects
def test_a_revert_of_a_valid_subject_is_accepted():
    assert cs.check_subject('Revert "feat(core): add a thing"') is None
    assert cs.check_subject('Revert "Revert "fix: x""') is None
    assert cs.check_subject('Reapply "fix: x"') is None
    assert cs.check_subject('Reapply "Revert "fix: x""') is None
    long_wrapper = 'Revert "fix: ' + "x" * 90 + '"'  # the wrapper may pass 100 characters, the quoted subject may not
    assert len(long_wrapper) > cs.MAX_LENGTH and cs.check_subject(long_wrapper) is None


@pytest.mark.parametrize("inner", ["Merge pull request #7 from someone/side", "Merge branch 'side' into main",
                                   "Merge remote-tracking branch 'origin/main' into side"])
def test_a_revert_or_reapply_of_a_merge_subject_is_accepted(inner: str):
    assert cs.check_subject(f'Revert "{inner}"') is None
    assert cs.check_subject(f'Reapply "{inner}"') is None
    assert cs.check_subject(f'Revert "Revert "{inner}""') is None


def test_a_revert_of_an_invalid_subject_is_rejected():
    for subject in ('Revert "Update README"', 'Revert ""', 'Revert "fix: ' + "x" * 100 + '"', 'Revert fix: x',
                    'revert "feat: x"', 'Reapply "Update README"', 'Reapply fix: x', 'Revert "Merge stuff"',
                    'Revert "Merge pull request #x from a/b"', 'Merge pull request #7 from someone/side'):
        assert cs.check_subject(subject) is not None, subject
    assert "reverted subject" in (cs.check_subject('Revert "Update README"') or "")


def test_git_revert_of_a_merge_and_of_a_revert_pass_the_gate(repo: Path):
    def change(name: str, subject: str) -> None:
        (repo / name).write_text(name, encoding="utf-8")
        _git(repo, "add", name)
        _git(repo, "commit", "-m", subject)

    change("root.txt", "feat: root")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    change("side.txt", "fix: side work")
    _git(repo, "checkout", "-q", "main")
    change("main.txt", "fix: main work")
    _git(repo, "merge", "--no-ff", "-m", "Merge pull request #7 from someone/side", "side")
    _git(repo, "revert", "--no-edit", "-m", "1", "HEAD")
    assert _git(repo, "log", "-1", "--format=%s") == 'Revert "Merge pull request #7 from someone/side"'
    _git(repo, "revert", "--no-edit", "HEAD")
    assert _git(repo, "log", "-1", "--format=%s").startswith(("Reapply ", 'Revert "Revert '))
    assert _bad(repo, base) == []


# ------------------------------------------------------------------ no attribution in commits
# The judgement is the repository's own guard (backend/aeos/core/attribution.py); these tests prove that the gate
# applies it to the message, the author and the committer, and prints its reasons. Every AI sample is assembled from
# parts so that the repository-wide hygiene scan has nothing to find in this file. TRAILER and AI_ADDR are defined
# above, with the pull request title tests.
GEN = "Generated" + " with"


def _bad(repo: Path, base: str) -> list[tuple[str, str, str]]:
    return cs.violations(f"{base}..HEAD", str(repo), baseline=None)


def test_the_gate_uses_the_canonical_attribution_guard():
    assert cs.find_ai_attribution is find_ai_attribution
    assert not hasattr(cs, "ATTRIBUTION_PATTERNS")
    assert yaml.safe_load(CI_TEXT)["jobs"]["commit-subjects"]["name"] == "commit subjects [G1]"


@pytest.mark.parametrize("body", [
    f"{TRAILER}: Claude <{AI_ADDR}>",
    f"{TRAILER.lower()}: Some Name <helper@openai.com>",
    f"{TRAILER.upper()}: Copilot <copilot@users.noreply.github.com>",
    f"{TRAILER}: Gemini <x@example.com>",
    f"{TRAILER}: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
    f"{TRAILER}: aider (gpt-4) <noreply@aider.chat>",
    f"{TRAILER}: devin-ai-integration[bot] <1+d@users.noreply.github.com>",
    f"{TRAILER}: Cl​aude <x@example.com>",  # zero-width space inside the name
    "Co-authored" + " by: Claude",
    "Signed-off-by: " + "Claude <x@example.com>",
    "Assisted-by: " + "Claude",
    "Generated-by: " + "ChatGPT",
    f"{GEN} [Claude Code](https://claude.com/claude-code)",
    GEN + " ChatGPT",
    "Generated" + "  with  " + "Claude",  # double spaces
    "Created" + " with Claude",
    "Written" + " by Codex",
    "Generated" + " using Windsurf",
    "Dibuat" + " dengan Claude Code",
])
def test_attribution_in_the_message_is_rejected(repo: Path, body: str):
    base = _commit(repo, "feat: root")
    sha = _commit(repo, f"fix(core): fine subject\n\nWhat changed.\n\n{body}")
    found = _bad(repo, base)
    assert [f[0] for f in found] == [sha]
    assert cs.check_subject("fix(core): fine subject") is None
    assert found[0][2].startswith("AI attribution in the message - line 5: ")  # the guard's own reason, not the subject


def test_attribution_in_a_merge_commit_message_is_rejected_but_the_merge_subject_is_exempt(repo: Path):
    base = _commit(repo, "feat: root")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "fix: side work")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "fix: main work")
    _git(repo, "merge", "--no-ff", "-m", f"Merge pull request #1\n\n{TRAILER}: Claude <{AI_ADDR}>", "side")
    found = _bad(repo, base)
    assert len(found) == 1 and found[0][1] == "Merge pull request #1" and "attribution trailer" in found[0][2]


def test_a_clean_merge_commit_and_a_human_co_author_pass(repo: Path):
    base = _commit(repo, "feat: root")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, f"fix: pair work\n\n{TRAILER}: Ada Lovelace <ada@example.com>")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "fix: main work")
    _git(repo, "merge", "--no-ff", "-m", "Merge pull request #2 from someone/side", "side")
    assert _bad(repo, base) == []


@pytest.mark.parametrize("who", ["git_author_email", "git_committer_email"])
@pytest.mark.parametrize("email", [AI_ADDR, "Bot@Mail.OpenAI.com"])
def test_an_ai_vendor_author_or_committer_email_is_rejected(repo: Path, who: str, email: str):
    base = _commit(repo, "feat: root")
    sha = _commit(repo, "feat: from a tool", **{who: email})
    found = _bad(repo, base)
    role = "author" if "author" in who else "committer"
    assert [f[0] for f in found] == [sha] and f"AI identity as {role}: the e-mail" in found[0][2]


@pytest.mark.parametrize("who", ["git_author_name", "git_committer_name"])
@pytest.mark.parametrize("name", ["Claude", "Copilot", "claude[bot]", "cursoragent", "dependabot[bot]"])
def test_an_ai_author_or_committer_name_with_a_normal_email_is_rejected(repo: Path, who: str, name: str):
    base = _commit(repo, "feat: root")
    sha = _commit(repo, "feat: from a tool", **{who: name})
    found = _bad(repo, base)
    role = "author" if "author" in who else "committer"
    assert [f[0] for f in found] == [sha] and f"AI identity as {role}: the name" in found[0][2]


# Ordinary prose about the product that the old private patterns flagged; the canonical guard must let it through.
PROSE = (
    "page tokens " + "generated with" + " a keyset cursor",
    "the file is " + "generated with" + " the Claude bridge",
    "the schema is " + "generated with" + " the OpenAI-compatible route shape",
    "The cursor moves; " + "generated with" + " care.",
)


@pytest.mark.parametrize("sentence", PROSE)
def test_ordinary_prose_that_mentions_tools_or_generation_passes(repo: Path, sentence: str):
    base = _commit(repo, "feat: root")
    _commit(repo, f"feat: cursors and geminis\n\n{sentence}\n\n{TRAILER}: Ada Lovelace <ada@example.com>",
            git_author_email="me@notanthropic.com")
    assert find_ai_attribution(sentence) == []
    assert _bad(repo, base) == []


def test_the_cli_prints_per_commit_ascii_reasons_for_attribution(repo: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]):
    base = _commit(repo, "feat: root")
    _commit(repo, f"feat: café\n\n{TRAILER}: Café Claude <x@example.com>")
    monkeypatch.chdir(repo)
    assert cs.main([f"{base}..HEAD", "--baseline", "none"]) == 1
    out = capsys.readouterr().out
    assert out.isascii() and "FAIL" in out and "attribution trailer" in out and "1 of 1" in out
    assert "caf\\xe9" in out  # non-ASCII is escaped, not dropped


# ------------------------------------------------------------------ NUL-separated log parsing
def _commit_message(repo: Path, message: str) -> str:
    """A commit whose message is written verbatim from a file (control characters survive; git forbids only NUL)."""
    msg = repo / ".." / "message.txt"
    msg.write_bytes(message.encode("utf-8"))
    _git(repo, "commit", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("separator", ["\x1e", "\x1f", "\x1e\x1f", "\x1f\x1e", "\x1e\x1e\x1f\x1f"])
def test_record_separator_characters_in_a_message_neither_hide_attribution_nor_crash(repo: Path, separator: str):
    base = _commit(repo, "feat: root")
    sha = _commit_message(repo, f"fix: x{separator}y\n\nmore{separator}\n\n{TRAILER}: Claude <x@example.com>\n")
    clean = _commit_message(repo, f"fix: z{separator}\n\nbody{separator}text\n")
    found = _bad(repo, base)
    assert [f[0] for f in found] == [sha] and "attribution trailer" in found[0][2]
    assert [c.sha for c in cs.log(f"{base}..HEAD", str(repo), baseline=None)] == [clean, sha]


def test_an_unparseable_git_log_is_exit_2_with_an_ascii_message_never_a_traceback(
        repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    base = _commit(repo, "feat: root")
    _commit(repo, "feat: next")
    monkeypatch.chdir(repo)
    for garbage in ("a\0b\0c\0", "x" * 40 + "\0" + "\0".join("f" * 5) + "\0", "\x1e\x1f", "not-a-sha\0" + "\0".join("123456")):
        real = cs._git

        def fake(args, cwd, garbage=garbage, real=real):
            proc = real(args, cwd)
            return subprocess.CompletedProcess(proc.args, 0, garbage, "") if args[0] == "log" else proc

        monkeypatch.setattr(cs, "_git", fake)
        assert cs.main([f"{base}..HEAD", "--baseline", "none"]) == 2
        out = capsys.readouterr().out
        assert out.isascii() and out.startswith("commit-subject: error: "), out
        monkeypatch.setattr(cs, "_git", real)


def test_an_empty_range_and_a_missing_git_are_clean_results(repo: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]):
    _commit(repo, "feat: root")
    monkeypatch.chdir(repo)
    assert cs.main(["HEAD..HEAD", "--baseline", "none"]) == 0
    assert "0 commit(s) checked" in capsys.readouterr().out
    monkeypatch.setenv("PATH", str(repo))  # no git
    assert cs.main(["HEAD..HEAD", "--baseline", "none"]) == 2
    assert capsys.readouterr().out.isascii()


# ------------------------------------------------------------------ a missing tool is a FAIL, never a skip
@pytest.mark.parametrize("tool, gate", [("mypy", "mypy-darwin"), ("mypy", "mypy-win32"), ("ruff", "ruff")])
def test_a_missing_python_tool_is_a_fail_with_a_clear_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                             tool: str, gate: str):
    real = importlib.util.find_spec
    monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name, *a: None if name == tool else real(name, *a))
    chosen = next(g for g in gates.STATIC_GATES if g.name == gate)
    result = gates.run_gate(chosen, tmp_path, gates.clean_env())
    assert result.status == gates.FAIL
    assert result.detail == (f"{tool} is not installed for {sys.executable}; use the project .venv "
                             "or set AEOS_GATES_PYTHON")
    assert "No module named" not in result.detail


def test_a_gate_that_is_not_a_python_tool_is_unaffected_by_the_tool_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name, *a: None)
    ok = gates.run_gate(gates.Gate("ok", ("python", "-c", "pass")), tmp_path, gates.clean_env())
    assert ok.status == gates.PASS


# ------------------------------------------------------------------ pre-push interpreter selection
HOOK = ROOT / "scripts" / "git-hooks" / "pre-push"
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="the pre-push hook is a POSIX sh script; no sh on this host")


def _stub(path: Path, label: str) -> Path:
    """A small executable FILE (never a symlink) that reports which interpreter the hook chose."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho \"CHOSEN={label} ARGS=$*\"\n", encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def linked(tmp_path: Path) -> tuple[Path, Path]:
    """(main checkout, linked worktree), both under directories with a space in the name; neither has a .venv."""
    main = tmp_path / "main checkout"
    main.mkdir()
    _git(main, "init", "-q")
    _commit(main, "feat: root")
    wt = tmp_path / "linked wt"
    _git(main, "worktree", "add", "-q", "--detach", str(wt))
    return main, wt


def _run_hook_script(cwd: Path, env_extra: dict[str, str] | None = None, path_prefix: Path | None = None):
    env = gates.clean_env()
    env.pop("AEOS_GATES_PYTHON", None)
    if path_prefix:
        env["PATH"] = str(path_prefix) + os.pathsep + env["PATH"]
    env.update(env_extra or {})
    return subprocess.run(["sh", str(HOOK), "origin", "url"], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", input="", check=False)


@needs_sh
def test_hook_uses_the_main_checkout_venv_from_a_linked_worktree(linked: tuple[Path, Path]):
    main, wt = linked
    _stub(main / ".venv" / "bin" / "python", "main-venv")
    proc = _run_hook_script(wt)
    assert proc.returncode == 0, proc.stderr
    assert "CHOSEN=main-venv" in proc.stdout and "--hook origin url" in proc.stdout and "warning" not in proc.stderr


@needs_sh
def test_hook_prefers_the_checkouts_own_venv_over_the_main_one(linked: tuple[Path, Path]):
    main, wt = linked
    _stub(main / ".venv" / "bin" / "python", "main-venv")
    _stub(wt / ".venv" / "bin" / "python", "own-venv")
    assert "CHOSEN=own-venv" in _run_hook_script(wt).stdout


@needs_sh
def test_aeos_gates_python_wins_and_a_bad_value_is_an_error(linked: tuple[Path, Path], tmp_path: Path):
    main, wt = linked
    _stub(main / ".venv" / "bin" / "python", "main-venv")
    chosen = _stub(tmp_path / "my tools" / "py", "override")
    proc = _run_hook_script(wt, {"AEOS_GATES_PYTHON": str(chosen)})
    assert proc.returncode == 0 and "CHOSEN=override" in proc.stdout
    bad = _run_hook_script(wt, {"AEOS_GATES_PYTHON": str(tmp_path / "missing" / "python")})
    assert bad.returncode == 1 and "AEOS_GATES_PYTHON" in bad.stderr and bad.stderr.isascii() and not bad.stdout
    not_exec = tmp_path / "plain.txt"
    not_exec.write_text("x", encoding="utf-8")
    not_exec.chmod(0o644)
    assert _run_hook_script(wt, {"AEOS_GATES_PYTHON": str(not_exec)}).returncode == 1


@needs_sh
def test_hook_falls_back_to_path_python_with_an_ascii_warning(linked: tuple[Path, Path], tmp_path: Path):
    _, wt = linked
    fake_bin = tmp_path / "fakebin"
    _stub(fake_bin / "python3", "path-python3")
    proc = _run_hook_script(wt, path_prefix=fake_bin)
    assert proc.returncode == 0 and "CHOSEN=path-python3" in proc.stdout
    assert "warning" in proc.stderr and "python3" in proc.stderr and proc.stderr.isascii()


@needs_sh
def test_hook_ignores_a_venv_next_to_a_bare_repository(tmp_path: Path):
    """git-common-dir of a worktree of a bare repository is not a ".git": its parent is unrelated and is skipped."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _commit(source, "feat: root")
    parent = tmp_path / "bare parent"
    parent.mkdir()
    _git(parent, "clone", "-q", "--bare", str(source), "bare.git")
    wt = parent / "wt"
    _git(parent / "bare.git", "worktree", "add", "-q", "--detach", str(wt))
    _stub(parent / ".venv" / "bin" / "python", "unrelated-venv")
    fake_bin = tmp_path / "fakebin"
    _stub(fake_bin / "python3", "path-python3")
    proc = _run_hook_script(wt, path_prefix=fake_bin)
    assert proc.returncode == 0, proc.stderr
    assert "CHOSEN=path-python3" in proc.stdout and "unrelated-venv" not in proc.stdout
