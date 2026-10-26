"""Wrong-commit and identity guards of the pre-push hook, the merge-conflict gate, the NOT RUN and verdict lines of
scripts/gates.py, scripts/preflight.py, scripts/run_tests.sh and scripts/run_with_deadline.py.

Every check has a BITE TEST: a violating fixture is red and a near-miss legitimate fixture is green. Hermetic: no
network, no push to a real remote. The pushes go from a throwaway clone to a throwaway bare repository under
tmp_path, with `core.hooksPath` set in that clone only; git never reads a global or system config here (an empty
file stands in for the global one) and every commit identity is a placeholder address. The tests that need `sh` (git
runs the hook through it, on Windows too: Git for Windows ships one) skip only where `sh` is not on PATH."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from aeos.core.paths import repo_root

ROOT = repo_root()
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ZERO = "0" * 40
ME = "dev@example.com"
OTHER = "someone.else@example.com"
# Assembled from parts so that the repository-wide hygiene scan has nothing to find in this file.
TRAILER = "Co-authored-by"
AI_ADDR = "noreply" + "@" + "anthropic.com"
NEEDS_SH = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh on PATH (git runs hooks through it)")
FAKE_BIN = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="a fake `gh` shell script cannot be started by name on Windows (NOT-PROVEN there)")
NEEDS_BASH = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="needs bash; on Windows (where bash may be the WSL launcher) this is NOT-PROVEN")


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aeos_script_{name}_safety", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gates = _load("gates")
preflight = _load("preflight")
deadline = _load("run_with_deadline")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git config (hooksPath, signing, templates) reaches any git call; no gh, no network."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(gates, "run_facts", lambda: [])


# ------------------------------------------------------------------ throwaway repositories
def _env(**identity: str) -> dict[str, str]:
    env = gates.clean_env()
    env.update(GIT_CONFIG_GLOBAL=os.environ.get("GIT_CONFIG_GLOBAL", os.devnull), GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL=ME, GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL=ME)
    env.update({k.upper(): v for k, v in identity.items()})  # git_author_email="x" -> GIT_AUTHOR_EMAIL
    return env


def _git(repo: Path, *args: str, check: bool = True, **identity: str) -> str:
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=repo,
                          env=_env(**identity), capture_output=True, text=True, encoding="utf-8", check=check)
    return proc.stdout.strip()


def _commit(repo: Path, subject: str, **identity: str) -> str:
    _git(repo, "commit", "--allow-empty", "-m", subject, **identity)
    return _git(repo, "rev-parse", "HEAD")


def _write_commit(repo: Path, name: str, text: str, subject: str, **identity: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    return _commit(repo, subject, **identity)


def _configure(repo: Path, email: str | None = ME, name: str | None = "Dev") -> None:
    if name is not None:
        _git(repo, "config", "user.name", name)
    if email is not None:
        _git(repo, "config", "user.email", email)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _configure(path)
    return path


def _merge_tree_available() -> bool:
    probe = subprocess.run(["git", "merge-tree", "--write-tree", "HEAD", "HEAD"], capture_output=True, text=True,
                           env=_env(), check=False)
    return "usage" not in probe.stderr.lower() and probe.returncode != 129


needs_merge_tree = pytest.mark.skipif(not _merge_tree_available(), reason="git older than 2.38: no merge-tree --write-tree")


def _pushref(ref: str, sha: str, remote_sha: str = ZERO) -> object:
    return gates.PushRef(ref, sha, ref, remote_sha)


# ------------------------------------------------------------------ wrong-commit guard (function level)
def test_a_detached_head_ahead_of_the_pushed_branch_is_refused_with_both_shas(repo: Path):
    stale = _commit(repo, "feat: pushed")
    _git(repo, "checkout", "-q", "--detach")
    newer = _commit(repo, "feat: only on the detached head")
    result = gates.wrong_commit_guard([_pushref("refs/heads/main", stale)], repo, gates.clean_env())
    assert result.status == gates.FAIL
    assert newer in result.detail and stale in result.detail
    assert "git branch -f main HEAD" in result.detail and "git merge-base --is-ancestor main HEAD" in result.detail
    assert result.detail.isascii()


@pytest.mark.parametrize("where", ["equal", "ancestor"])
def test_a_detached_head_that_the_push_carries_is_allowed(repo: Path, where: str):
    first = _commit(repo, "feat: first")
    second = _commit(repo, "feat: second")
    _git(repo, "checkout", "-q", "--detach", first if where == "ancestor" else second)
    result = gates.wrong_commit_guard([_pushref("refs/heads/main", second)], repo, gates.clean_env())
    assert result.status == gates.PASS


def test_a_detached_head_on_an_unrelated_commit_is_refused(repo: Path):
    _commit(repo, "feat: base")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "feat: side")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "--detach")
    elsewhere = _commit(repo, "feat: elsewhere")
    result = gates.wrong_commit_guard([_pushref("refs/heads/side", side)], repo, gates.clean_env())
    assert result.status == gates.FAIL and elsewhere in result.detail and side in result.detail


def test_pushing_another_branch_from_an_attached_clean_head_is_allowed(repo: Path):
    _commit(repo, "feat: base")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic = _commit(repo, "feat: topic")
    _git(repo, "checkout", "-q", "main")
    result = gates.wrong_commit_guard([_pushref("refs/heads/topic", topic)], repo, gates.clean_env())
    assert result.status == gates.PASS


def test_a_pushed_ref_of_the_current_branch_that_is_not_head_is_refused_with_both_shas(repo: Path):
    old = _commit(repo, "feat: old")
    head = _commit(repo, "feat: head")
    result = gates.wrong_commit_guard([_pushref("refs/heads/main", old)], repo, gates.clean_env())
    assert result.status == gates.FAIL and old in result.detail and head in result.detail
    assert gates.wrong_commit_guard([_pushref("refs/heads/main", head)], repo, gates.clean_env()).status == gates.PASS


def test_deletions_only_and_an_unborn_head_are_not_judged(repo: Path):
    assert gates.wrong_commit_guard([_pushref("refs/heads/gone", ZERO, "a" * 40)], repo, gates.clean_env()) is None
    unborn = gates.wrong_commit_guard([_pushref("refs/heads/x", "b" * 40)], repo, gates.clean_env())
    assert unborn is not None and unborn.status == gates.SKIPPED and "no commit" in unborn.detail


# ------------------------------------------------------------------ identity guard (function level)
def _guard(repo: Path, base: str | None, tip: str | None = None):
    tip = tip or _git(repo, "rev-parse", "HEAD")
    return gates.identity_guard(gates.PushRef("refs/heads/topic", tip, "refs/heads/topic", ZERO), base, repo,
                                gates.clean_env())


def test_commits_by_the_configured_identity_pass_and_the_address_match_ignores_case(repo: Path):
    base = _commit(repo, "feat: base")
    _commit(repo, "feat: mine")
    _commit(repo, "feat: mine too", git_author_email=ME.upper(), git_committer_email=ME.upper())
    assert _guard(repo, base).status == gates.PASS


@pytest.mark.parametrize("who, side", [("git_author_email", "author"), ("git_committer_email", "committer")])
def test_a_commit_by_another_address_is_refused_with_the_fix_and_no_address(repo: Path, who: str, side: str):
    base = _commit(repo, "feat: base")
    bad = _commit(repo, "feat: wrong identity", **{who: OTHER})
    result = _guard(repo, base)
    assert result.status == gates.FAIL
    assert bad[:12] in result.detail and "COMMIT_IDENTITY_MISMATCH" in result.detail and side in result.detail
    assert "git config user.email" in result.detail and "--reset-author" in result.detail
    assert OTHER not in result.detail and ME not in result.detail and result.detail.isascii()


def _printed_rebase(detail: str) -> list[str]:
    """The `git rebase ...` line of the guard's message, as the argv a shell would make of it."""
    (line,) = [ln.strip() for ln in detail.splitlines() if ln.strip().startswith("git rebase ")]
    return shlex.split(line)[1:]


def test_the_printed_commands_are_paste_safe_in_zsh_and_cmd(repo: Path):
    """cmd.exe splits an argument in single quotes at '||', and zsh passes an inline '#' comment on to the command:
    the printed commands use double quotes and carry no comment."""
    base = _commit(repo, "feat: base")
    _commit(repo, "feat: wrong identity", git_author_email=OTHER)
    detail = _guard(repo, base).detail
    commands = [ln.strip() for ln in detail.splitlines() if ln.strip().startswith("git ")]
    assert len(commands) == 2
    for command in commands:
        assert "#" not in command and "'" not in command, command
    rebase = _printed_rebase(detail)
    assert rebase[-2] == "--exec" and rebase[-1] == gates.AMEND_EXEC  # one argument, "||" and all


def test_the_fix_the_message_prints_makes_the_guard_pass(repo: Path):
    base = _commit(repo, "feat: base")
    _commit(repo, "feat: wrong identity", git_author_email=OTHER)
    result = _guard(repo, base)
    assert result.status == gates.FAIL
    _git(repo, *_printed_rebase(result.detail))
    assert _guard(repo, base).status == gates.PASS


def test_the_printed_fix_leaves_the_commits_that_came_from_origin_main_alone(repo: Path):
    """A branch that merged origin/main (a commit by someone else) and then made one commit under a wrong address:
    the guard lists only the last commit, and its printed fix must re-author only that one, keeping the other
    person's commit as it is on main and the branch a descendant of origin/main."""
    _write_commit(repo, "root.txt", "root\n", "feat: root")
    _git(repo, "checkout", "-q", "-b", "topic")
    remote_tip = _commit(repo, "feat: already on the remote")
    _git(repo, "checkout", "-q", "main")
    landed = _commit(repo, "fix: landed by other", git_author_name="Other", git_author_email=OTHER,
                     git_committer_name="Other", git_committer_email=OTHER)
    _git(repo, "update-ref", "refs/remotes/origin/main", landed)
    _git(repo, "checkout", "-q", "topic")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge remote-tracking branch 'origin/main' into topic", "origin/main")
    wrong = _commit(repo, "feat: wrong author", git_author_email=OTHER)
    result = _guard(repo, remote_tip)
    assert result.status == gates.FAIL and wrong[:12] in result.detail and landed[:12] not in result.detail
    _git(repo, *_printed_rebase(result.detail))
    assert _guard(repo, remote_tip).status == gates.PASS
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=repo, env=_env(),
                              check=False)
    assert ancestor.returncode == 0  # still carries main's own commit, not a copy of it
    assert _git(repo, "log", "-1", "--format=%an <%ae> %cn <%ce>", landed) == f"Other <{OTHER}> Other <{OTHER}>"
    assert _git(repo, "log", "--format=%H", "origin/main..HEAD", "--author", OTHER) == ""  # nothing re-authored away


def test_the_plain_rebase_without_the_main_exemption_would_have_rewritten_main_s_commit(repo: Path):
    """Near-miss that shows the test above bites: the old fix text (no -r, no exemption) copies main's commit."""
    _write_commit(repo, "root.txt", "root\n", "feat: root")
    _git(repo, "checkout", "-q", "-b", "topic")
    remote_tip = _commit(repo, "feat: already on the remote")
    _git(repo, "checkout", "-q", "main")
    landed = _commit(repo, "fix: landed by other", git_author_email=OTHER, git_committer_email=OTHER)
    _git(repo, "update-ref", "refs/remotes/origin/main", landed)
    _git(repo, "checkout", "-q", "topic")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge remote-tracking branch 'origin/main' into topic", "origin/main")
    _commit(repo, "feat: wrong author", git_author_email=OTHER)
    _git(repo, "rebase", remote_tip, "--exec", "git commit --amend --no-edit --reset-author --allow-empty")
    cut_loose = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=repo, env=_env(),
                               check=False)
    assert cut_loose.returncode != 0


@pytest.mark.parametrize("name", ["Copilot", "claude[bot]"])
def test_an_ai_author_name_is_refused_even_with_the_right_address(repo: Path, name: str):
    base = _commit(repo, "feat: base")
    _commit(repo, "feat: from a tool", git_author_name=name)
    result = _guard(repo, base)
    assert result.status == gates.FAIL and "AI_ATTRIBUTION_IN_COMMIT" in result.detail


def test_an_ai_attribution_in_the_message_is_refused(repo: Path):
    base = _commit(repo, "feat: base")
    _commit(repo, f"feat: x\n\n{TRAILER}: Claude <{AI_ADDR}>")
    result = _guard(repo, base)
    assert result.status == gates.FAIL and "AI_ATTRIBUTION_IN_COMMIT" in result.detail and AI_ADDR not in result.detail


def test_commits_already_on_origin_main_are_not_the_pushes_to_check(repo: Path):
    """A merge of origin/main brings in commits of other identities (a host writes its merge commits); only the
    commits this push adds are judged."""
    base = _commit(repo, "feat: base")
    _git(repo, "checkout", "-q", "-b", "topic")
    remote_topic = _commit(repo, "feat: topic")
    _git(repo, "checkout", "-q", "main")
    landed = _commit(repo, "fix: landed by someone else", git_author_email=OTHER, git_committer_email=OTHER)
    _git(repo, "update-ref", "refs/remotes/origin/main", landed)
    _git(repo, "checkout", "-q", "topic")
    _git(repo, "merge", "--no-ff", "-m", "Merge remote-tracking branch 'origin/main' into topic", "origin/main")
    mine = _commit(repo, "feat: more")
    result = _guard(repo, remote_topic, mine)
    assert result.status == gates.PASS, result.detail
    # bite: the same foreign commit WITHOUT origin/main knowing it is a push of that commit
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert _guard(repo, remote_topic, mine).status == gates.FAIL
    assert base  # the first commit stays outside every range above


def test_an_unusable_repository_identity_is_refused_but_an_empty_range_needs_none(tmp_path: Path):
    bare_cfg = tmp_path / "noid"
    bare_cfg.mkdir()
    _git(bare_cfg, "init", "-q")
    base = _commit(bare_cfg, "feat: base")
    assert _guard(bare_cfg, base, base).status == gates.PASS  # nothing is pushed, so no identity is needed
    _commit(bare_cfg, "feat: next")
    missing = _guard(bare_cfg, base)
    assert missing.status == gates.FAIL and "name is missing" in missing.detail
    _configure(bare_cfg, email=None)  # a name without an address
    no_email = _guard(bare_cfg, base)
    assert no_email.status == gates.FAIL and "email is missing" in no_email.detail
    _configure(bare_cfg, email=AI_ADDR)
    ai = _guard(bare_cfg, base)
    assert ai.status == gates.FAIL and "not usable" in ai.detail and AI_ADDR not in ai.detail
    _configure(bare_cfg)
    assert _guard(bare_cfg, base).status == gates.PASS


def test_a_push_with_no_known_base_is_skipped_with_the_reason(repo: Path):
    _commit(repo, "feat: base")
    result = _guard(repo, None)
    assert result.status == gates.SKIPPED and "no base" in result.detail


# ------------------------------------------------------------------ the merge-conflict gate
def _conflict_repo(repo: Path, *, conflicting: bool) -> None:
    _write_commit(repo, "a.txt", "one\n", "feat: base")
    _git(repo, "checkout", "-q", "-b", "topic")
    if conflicting:
        _write_commit(repo, "a.txt", "topic\n", "feat: topic edits a")
    else:
        _write_commit(repo, "b.txt", "topic\n", "feat: topic adds b")
    _git(repo, "checkout", "-q", "main")
    main = _write_commit(repo, "a.txt", "main\n", "feat: main edits a")
    _git(repo, "update-ref", "refs/remotes/origin/main", main)
    _git(repo, "checkout", "-q", "topic")


@needs_merge_tree
def test_a_branch_that_conflicts_with_origin_main_fails_and_lists_the_files(repo: Path):
    _conflict_repo(repo, conflicting=True)
    result = gates.merge_conflict_result(repo, gates.clean_env())
    assert result.status == gates.FAIL
    assert "a.txt" in result.detail and "1 file(s)" in result.detail and "git merge origin/main" in result.detail
    assert result.detail.isascii()


@needs_merge_tree
def test_a_branch_that_merges_cleanly_passes(repo: Path):
    _conflict_repo(repo, conflicting=False)
    assert gates.merge_conflict_result(repo, gates.clean_env()).status == gates.PASS


@needs_merge_tree
def test_the_merge_conflict_gate_is_skipped_with_a_reason_without_origin_main(repo: Path):
    _commit(repo, "feat: base")
    result = gates.merge_conflict_result(repo, gates.clean_env())
    assert result.status == gates.SKIPPED and "origin/main" in result.detail


@needs_merge_tree
def test_a_merge_that_git_cannot_make_at_all_is_a_fail_with_gits_message_not_a_skip(repo: Path):
    """Unrelated histories: merge-tree exits 128 (git 2.39+) or 1 (2.38); either way the branch cannot be merged."""
    _commit(repo, "feat: base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "island")
    _write_commit(repo, "island.txt", "x\n", "feat: unrelated root")
    result = gates.merge_conflict_result(repo, gates.clean_env())
    assert result.status == gates.FAIL, result.detail
    assert "unrelated" in result.detail.lower() or "island.txt" in result.detail or "conflict" in result.detail.lower()
    assert "needs git 2.38" not in result.detail and result.detail.isascii()


def _fake_git(code: int, stderr: str = "", stdout: str = ""):
    def run(args, cwd, env=None):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "abc\n", "")
        return subprocess.CompletedProcess(args, code, stdout, stderr)
    return run


def test_only_the_usage_error_of_an_old_git_is_a_skip_every_other_failure_is_a_fail(
        repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gates, "git", _fake_git(129, "usage: git merge-tree <base-tree> <branch1> <branch2>"))
    old = gates.merge_conflict_result(repo, gates.clean_env())
    assert old.status == gates.SKIPPED and "2.38" in old.detail
    monkeypatch.setattr(gates, "git", _fake_git(128, "fatal: refusing to merge unrelated histories"))
    broken = gates.merge_conflict_result(repo, gates.clean_env())
    assert broken.status == gates.FAIL and "refusing to merge unrelated histories" in broken.detail
    assert "exit 128" in broken.detail and "2.38" not in broken.detail
    monkeypatch.setattr(gates, "git", _fake_git(1, "", "treeid\nsrc/a.py\n"))
    assert gates.merge_conflict_result(repo, gates.clean_env()).status == gates.FAIL
    monkeypatch.setattr(gates, "git", _fake_git(0, "", "treeid\n"))
    assert gates.merge_conflict_result(repo, gates.clean_env()).status == gates.PASS


@needs_merge_tree
def test_a_local_run_fails_on_a_conflict_prints_the_verdict_last_and_exits_1(
        repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    _conflict_repo(repo, conflicting=True)
    trivial = gates.Gate("probe", ("python", "-c", "pass"))
    monkeypatch.setattr(gates, "STATIC_GATES", (trivial,))
    monkeypatch.setattr(gates, "FRONTEND_GATES", ())
    real = gates.run_local
    monkeypatch.setattr(gates, "run_local", lambda args: real(args, repo))
    monkeypatch.setattr(gates, "commit_subject_gate", lambda base, head: trivial)
    assert gates.main([]) == 1
    out = capsys.readouterr().out
    assert "merge-conflict" in out and "a.txt" in out
    last = out.strip().splitlines()[-1]
    assert last.startswith("GATES: FAIL (3 run, 1 failed, ") and last.endswith(" not run)")


# ------------------------------------------------------------------ NOT RUN lines and the verdict line
def test_job_ids_are_read_from_the_workflow_without_a_yaml_library():
    assert gates.ci_job_ids(ROOT) == list(yaml.safe_load(CI_TEXT)["jobs"])
    assert gates.ci_job_ids(ROOT / "no-such-dir") == []


def test_the_commit_subjects_job_counts_as_run_only_when_both_its_gates_ran():
    jobs = list(yaml.safe_load(CI_TEXT)["jobs"])
    nothing = gates.not_run_jobs([], ROOT)
    assert [job for job, _ in nothing] == jobs  # no gate ran: every job is NOT RUN
    both = [gates.Result("commit-subjects", gates.PASS), gates.Result("shellcheck", gates.FAIL, 0.0, "x")]
    ran = gates.not_run_jobs(both, ROOT)
    assert [job for job, _ in ran] == [j for j in jobs if j != "commit-subjects"]
    # near misses: one of the two missing or skipped keeps the job NOT RUN and names the missing gate
    only_subjects = dict(gates.not_run_jobs([gates.Result("commit-subjects", gates.PASS)], ROOT))
    assert "shellcheck" in only_subjects["commit-subjects"]
    no_shellcheck = dict(gates.not_run_jobs([gates.Result("commit-subjects", gates.PASS),
                                             gates.Result("shellcheck", gates.SKIPPED, 0.0, "not on PATH")], ROOT))
    assert "shellcheck" in no_shellcheck["commit-subjects"] and "SKIPPED" in no_shellcheck["commit-subjects"]
    skipped = dict(gates.not_run_jobs([gates.Result("commit-subjects", gates.SKIPPED, 0.0, "no base"),
                                       gates.Result("shellcheck", gates.PASS)], ROOT))
    assert "commit-subjects" in skipped["commit-subjects"]
    reasons = dict(nothing)
    for prefix in ("backend-shards-windows-latest", "backend-verifier-macos-latest", "backend-macos-latest", "frontend",
                   "browser-e2e", "bootstrap", "supply-chain"):
        assert reasons[prefix] and reasons[prefix] != "no local equivalent", prefix


def test_a_job_added_to_the_workflow_shows_up_as_not_run_by_itself(tmp_path: Path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(CI_TEXT + "\n  brand-new-job:\n    runs-on: ubuntu-latest\n    steps: []\n", encoding="utf-8")
    found = dict(gates.not_run_jobs([], tmp_path))
    assert found["brand-new-job"] == "no local equivalent"
    plain = tmp_path / "plain"
    (plain / ".github" / "workflows").mkdir(parents=True)
    (plain / ".github" / "workflows" / "ci.yml").write_text(CI_TEXT, encoding="utf-8")
    assert "brand-new-job" not in dict(gates.not_run_jobs([], plain))


def test_the_workflow_missing_is_one_honest_line(tmp_path: Path):
    assert gates.not_run_jobs([], tmp_path)[0][0] == "all CI jobs"


def test_the_verdict_counts_run_failed_and_not_run():
    results = [gates.Result("a", gates.PASS), gates.Result("b", gates.FAIL), gates.Result("c", gates.SKIPPED, 0.0, "x")]
    assert gates.verdict_of(results, [("job1", "r"), ("job2", "r")]).line == "GATES: FAIL (2 run, 1 failed, 3 not run)"
    ok = gates.verdict_of([gates.Result("a", gates.PASS)], [])
    assert ok.line == "GATES: PASS (1 run, 0 failed, 0 not run)" and ok.status == gates.PASS


def test_the_output_ends_with_exactly_one_verdict_line_after_the_not_run_lines(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(gates, "run_local", lambda args: [gates.Result("a", gates.PASS, 0.1),
                                                          gates.Result("b", gates.SKIPPED, 0.0, "because")])
    assert gates.main([]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert sum(line.startswith("GATES:") for line in lines) == 1 and lines[-1].startswith("GATES: PASS (1 run, 0 failed, ")
    not_run = [line for line in lines if line.startswith("NOT RUN: ")]
    assert len(not_run) == len(yaml.safe_load(CI_TEXT)["jobs"])
    assert all(line.endswith(")") and line.isascii() for line in not_run)
    assert lines.index(not_run[-1]) < len(lines) - 1


def test_the_report_file_carries_the_verdict_and_stdout_stays_one_json_document(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(gates, "run_local", lambda args: [gates.Result("a", gates.FAIL, 0.1, "exit 1\nboom", "cmd")])
    report = tmp_path / "deep" / "dir" / "gates.json"
    assert gates.main(["--json", "--report", str(report)]) == 1
    captured = capsys.readouterr()
    on_stdout = json.loads(captured.out)
    on_disk = json.loads(report.read_text(encoding="utf-8"))
    assert on_stdout == on_disk and on_disk["ok"] is False
    assert on_disk["verdict"].startswith("GATES: FAIL (1 run, 1 failed, ")
    assert captured.err.strip() == on_disk["verdict"]
    assert on_disk["counts"]["failed"] == 1 and on_disk["not_run_jobs"] and report.read_bytes().isascii()
    assert gates.main(["--report", str(tmp_path)]) == 2  # a report that cannot be written is an error, not silence


def test_the_verdict_is_the_last_line_of_json_output_when_stderr_is_merged_into_stdout(tmp_path: Path):
    """`gates.py --json 2>&1 | tail`: stdout to a pipe is block-buffered, stderr is not, so without a flush of stdout
    first the verdict would come out before the JSON document."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('g', {str(ROOT / 'scripts' / 'gates.py')!r})\n"
        "g = importlib.util.module_from_spec(spec); sys.modules['g'] = g; spec.loader.exec_module(g)\n"
        "g.run_local = lambda args: [g.Result('a', g.PASS, 0.1)]\n"
        "sys.exit(g.main(['--json', '--no-facts']))\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(driver)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", check=False, timeout=120)
    lines = proc.stdout.strip().splitlines()
    assert proc.returncode == 0 and lines[-1].startswith("GATES: PASS (1 run, 0 failed, "), proc.stdout
    assert sum(line.startswith("GATES:") for line in lines) == 1
    assert json.loads("\n".join(lines[:-1]))["verdict"] == lines[-1]  # everything before it is the one JSON document


def _repo_with_scripts(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts" / "git-hooks").mkdir(parents=True)
    (root / "scripts" / "b.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "scripts" / "a.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "scripts" / "tool.py").write_text("", encoding="utf-8")
    (root / "scripts" / "git-hooks" / "pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_the_shellcheck_gate_runs_ci_s_files_and_fails_when_shellcheck_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo_with_scripts(tmp_path)
    assert gates.shell_files(root) == ["scripts/a.sh", "scripts/b.sh", "scripts/git-hooks/pre-push"]
    assert gates.SHELLCHECK_COMMAND == SHELLCHECK_CMD  # the same command as the CI step
    seen: list[list[str]] = []

    def stand_in(code: int):
        def resolve(gate):
            seen.append(list(gate.argv))
            return [sys.executable, "-c", f"raise SystemExit({code})"]
        return resolve

    monkeypatch.setattr(gates, "resolve_argv", stand_in(1))
    red = gates.shellcheck_result(root, gates.clean_env())
    assert red.status == gates.FAIL and "exit 1" in red.detail
    monkeypatch.setattr(gates, "resolve_argv", stand_in(0))
    green = gates.shellcheck_result(root, gates.clean_env())
    assert green.status == gates.PASS
    assert seen[-1] == ["shellcheck", "scripts/a.sh", "scripts/b.sh", "scripts/git-hooks/pre-push"]


def test_the_shellcheck_gate_is_skipped_with_a_reason_without_the_tool_or_without_scripts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo_with_scripts(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools-here"))
    missing = gates.shellcheck_result(root, gates.clean_env())
    assert missing.status == gates.SKIPPED and "not on PATH" in missing.detail
    empty = tmp_path / "empty"
    empty.mkdir()
    none = gates.shellcheck_result(empty, gates.clean_env())
    assert none.status == gates.SKIPPED and "no shell scripts" in none.detail


def test_a_local_run_includes_the_shellcheck_gate_before_the_commit_subject_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(gates, "STATIC_GATES", ())
    monkeypatch.setattr(gates, "FRONTEND_GATES", ())
    monkeypatch.setattr(gates, "shellcheck_result", lambda root, env, ref="": seen.append(root) or gates.Result(
        "shellcheck", gates.SKIPPED, 0.0, "stand-in"))
    args = gates.build_parser().parse_args(["--base", "none-such"])
    results = gates.run_local(args, tmp_path)
    assert [r.gate for r in results].index("shellcheck") < [r.gate for r in results].index("commit-subjects")
    assert seen == [tmp_path]


def test_environment_facts_are_printed_first_and_do_not_change_the_result(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(gates, "run_facts", lambda: ["python: /x/python (3.13.0)", "gh_auth: ok"])
    monkeypatch.setattr(gates, "run_local", lambda args: [gates.Result("a", gates.PASS)])
    assert gates.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("environment (informational):\n  python: /x/python") and "gh_auth: ok" in out
    assert gates.main(["--no-facts"]) == 0 and "environment" not in capsys.readouterr().out


def test_facts_that_cannot_be_collected_never_fail_a_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.undo()  # the real run_facts, with a preflight that does not exist
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setattr(gates, "PREFLIGHT", Path("no-such-preflight.py"))
    lines = gates.run_facts()
    assert lines and lines[0].startswith("preflight: facts unavailable")


# ------------------------------------------------------------------ preflight --facts
def _path_without(tool: str) -> str:
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / tool).exists() and not (Path(d) / f"{tool}.exe").exists()]
    return os.pathsep.join(keep)


def test_facts_are_ascii_key_value_lines_and_exit_zero(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", _path_without("gh"))
    assert preflight.main(["--facts"]) == 0
    lines = capsys.readouterr().out.splitlines()
    keys = [line.split(": ", 1)[0] for line in lines]
    assert keys == ["python", "project_venv", "pytest_importable", "xdist_importable", "node", "npm", "git",
                    "core.hooksPath", "shell", "gh_auth"]
    facts = dict(line.split(": ", 1) for line in lines)
    assert sys.executable in facts["python"] and facts["pytest_importable"] == "yes"
    assert facts["gh_auth"] == "gh not found" and all(line.isascii() for line in lines)


def test_facts_without_the_flag_do_nothing_and_exit_two(capsys: pytest.CaptureFixture[str]):
    assert preflight.main([]) == 2
    assert capsys.readouterr().out == ""


def test_a_failing_collector_still_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def boom(*args, **kwargs):
        raise RuntimeError("tool exploded ✓")

    monkeypatch.setattr(preflight, "collect_facts", boom)
    assert preflight.main(["--facts"]) == 0
    out = capsys.readouterr().out
    assert "facts unavailable" in out and out.isascii()


def test_missing_tools_are_reported_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert preflight.tool_version("node") == "not found" and preflight.gh_auth() == "gh not found"


@FAKE_BIN
@pytest.mark.parametrize("exit_code, expected", [(0, "ok"), (1, "not authenticated")])
def test_gh_auth_uses_only_the_exit_status_and_never_shows_a_token(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, expected: str):
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho 'Logged in with token {secret}'\nexit {exit_code}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    assert preflight.gh_auth() == expected
    assert secret not in preflight.render_facts(preflight.collect_facts())


def test_the_project_venv_fact_is_yes_only_for_the_checkout_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_root = tmp_path / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    monkeypatch.setattr(preflight.sys, "prefix", str(fake_root / ".venv"))
    assert preflight.project_venv(fake_root).startswith("yes (")
    monkeypatch.setattr(preflight.sys, "prefix", str(tmp_path / "somewhere else"))
    assert preflight.project_venv(fake_root).startswith("no (")


# ------------------------------------------------------------------ real `git push` through the pre-push hook
HOOK_WRAPPER = '''"""Test stand-in for scripts/git-hooks/pre-push: the real scripts/gates.py in hook mode, with the slow static
gates (ruff, mypy, secret scan) swapped for one trivial gate and the commit-message gate told that the clone has no
baseline. Everything else (guards, merge-conflict gate, worktree, verdict) is the real code."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("real_gates", {gates_path!r})
gates = importlib.util.module_from_spec(spec)
sys.modules["real_gates"] = gates
spec.loader.exec_module(gates)
gates.STATIC_GATES = (gates.Gate("probe", ("python", "-c", "pass")),)
gates.commit_subject_gate = lambda base, head: gates.Gate(
    "commit-subjects", ("python", str(gates.COMMIT_SUBJECT), f"{{base}}..{{head}}", "--baseline", "none"))
sys.exit(gates.main(sys.argv[1:]))
'''


class Rig:
    """A working clone, a bare remote and the hook, all under tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.remote = tmp_path / "remote.git"
        self.work = tmp_path / "work"
        self.hooks = tmp_path / "hooks"
        self.remote.mkdir()
        self.work.mkdir()
        self.hooks.mkdir()
        _git(self.remote, "init", "-q", "--bare")
        _git(self.work, "init", "-q")
        _configure(self.work)
        _git(self.work, "remote", "add", "origin", str(self.remote))
        _commit(self.work, "feat: root")
        _git(self.work, "push", "-q", "origin", "main")  # before the hook exists
        wrapper = tmp_path / "hook_wrapper.py"
        wrapper.write_text(HOOK_WRAPPER.format(gates_path=str(ROOT / "scripts" / "gates.py")), encoding="utf-8")
        hook = self.hooks / "pre-push"
        hook.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{wrapper.as_posix()}" '
                        '--hook --no-facts "$@"\n', encoding="utf-8", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _git(self.work, "config", "core.hooksPath", str(self.hooks))  # this clone only

    def push(self, *refspec: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "push", "origin", *refspec], cwd=self.work, env=_env(), capture_output=True,
                              text=True, encoding="utf-8", check=False)

    def remote_sha(self, ref: str) -> str | None:
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=self.remote, env=_env(),
                              capture_output=True, text=True, check=False)
        return proc.stdout.strip() or None

    def head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD")

    def run_hook_with_stdin(self, line: str) -> subprocess.CompletedProcess[str]:
        """The hook as git calls it (cwd = the working tree, push lines on stdin), for a line git itself cannot
        produce from an attached HEAD."""
        return subprocess.run(["sh", str(self.hooks / "pre-push"), "origin", str(self.remote)], cwd=self.work, env=_env(),
                              input=line, capture_output=True, text=True, encoding="utf-8", check=False)


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


@NEEDS_SH
def test_a_normal_push_goes_through_the_hook_and_arrives(rig: Rig):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    tip = _commit(rig.work, "feat: new work")
    proc = rig.push("feat")
    assert proc.returncode == 0, _combined(proc)
    assert "GATES: PASS" in _combined(proc) and "wrong-commit" in _combined(proc)
    assert rig.remote_sha("refs/heads/feat") == tip


@NEEDS_SH
def test_another_branch_from_a_clean_attached_head_is_allowed(rig: Rig):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    tip = _commit(rig.work, "feat: new work")
    _git(rig.work, "checkout", "-q", "main")
    proc = rig.push("feat")
    assert proc.returncode == 0, _combined(proc)
    assert rig.remote_sha("refs/heads/feat") == tip


@NEEDS_SH
def test_a_detached_head_with_an_unpushed_commit_is_refused_and_the_printed_fix_works(rig: Rig):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    stale = _commit(rig.work, "feat: pushed branch tip")
    _git(rig.work, "checkout", "-q", "--detach")
    newer = _commit(rig.work, "feat: made on the detached head")
    proc = rig.push("feat")
    text = _combined(proc)
    assert proc.returncode != 0 and "GATES: FAIL" in text
    assert newer in text and stale in text  # both shas
    assert "git branch -f feat HEAD" in text and "git merge-base --is-ancestor feat HEAD" in text
    assert rig.remote_sha("refs/heads/feat") is None  # nothing left the machine
    # the fix, exactly as printed, then the same push is allowed and carries the new commit
    _git(rig.work, "merge-base", "--is-ancestor", "feat", "HEAD")
    _git(rig.work, "branch", "-f", "feat", "HEAD")
    _git(rig.work, "switch", "-q", "feat")
    again = rig.push("feat")
    assert again.returncode == 0, _combined(again)
    assert rig.remote_sha("refs/heads/feat") == newer


@NEEDS_SH
def test_a_detached_head_that_the_pushed_branch_carries_is_allowed(rig: Rig):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    tip = _commit(rig.work, "feat: new work")
    _git(rig.work, "checkout", "-q", "--detach")
    proc = rig.push("feat")
    assert proc.returncode == 0, _combined(proc)
    assert rig.remote_sha("refs/heads/feat") == tip


@NEEDS_SH
def test_a_stale_branch_ref_is_refused_with_both_shas(rig: Rig):
    """git never hands the hook a line like this from an attached HEAD, so the hook is called as git calls it, with a
    push line whose sha is the branch's old tip while HEAD of that branch has moved on."""
    _git(rig.work, "checkout", "-q", "-b", "feat")
    old = _commit(rig.work, "feat: old tip")
    head = _commit(rig.work, "feat: current tip")
    proc = rig.run_hook_with_stdin(f"refs/heads/feat {old} refs/heads/feat {ZERO}\n")
    text = _combined(proc)
    assert proc.returncode == 1 and old in text and head in text and "stale" in text
    assert "GATES: FAIL" in text
    ok = rig.run_hook_with_stdin(f"refs/heads/feat {head} refs/heads/feat {ZERO}\n")  # near miss: the real tip
    assert ok.returncode == 0, _combined(ok)


@NEEDS_SH
@pytest.mark.parametrize("who", ["git_author_email", "git_committer_email"])
def test_a_commit_by_the_wrong_address_is_refused_and_amending_it_unblocks_the_push(rig: Rig, who: str):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    bad = _commit(rig.work, "feat: wrong identity", **{who: OTHER})
    proc = rig.push("feat")
    text = _combined(proc)
    assert proc.returncode != 0 and bad[:12] in text and "COMMIT_IDENTITY_MISMATCH" in text
    assert "git config user.email" in text and "--reset-author" in text
    assert OTHER not in text and ME not in text  # an address is never printed
    assert rig.remote_sha("refs/heads/feat") is None
    _git(rig.work, "commit", "--amend", "--no-edit", "--reset-author", "--allow-empty")
    fixed = rig.push("feat")
    assert fixed.returncode == 0, _combined(fixed)
    assert rig.remote_sha("refs/heads/feat") == rig.head()


@NEEDS_SH
def test_a_conflicting_branch_is_refused_by_the_hook_and_a_clean_one_is_not(rig: Rig):
    if not _merge_tree_available():
        pytest.skip("git older than 2.38: no merge-tree --write-tree")
    _git(rig.work, "checkout", "-q", "-b", "feat")
    _write_commit(rig.work, "a.txt", "feat\n", "feat: feat adds a")
    _git(rig.work, "checkout", "-q", "main")
    _write_commit(rig.work, "a.txt", "main\n", "feat: main adds a")
    assert rig.push("main").returncode == 0  # origin/main moves
    _git(rig.work, "checkout", "-q", "feat")
    proc = rig.push("feat")
    assert proc.returncode != 0 and "merge-conflict" in _combined(proc) and "a.txt" in _combined(proc)
    assert rig.remote_sha("refs/heads/feat") is None
    _git(rig.work, "merge", "-q", "-X", "ours", "-m", "Merge remote-tracking branch 'origin/main' into feat", "origin/main")
    resolved = rig.push("feat")
    assert resolved.returncode == 0, _combined(resolved)


@NEEDS_SH
def test_a_deletion_is_not_judged(rig: Rig):
    _git(rig.work, "checkout", "-q", "-b", "feat")
    _commit(rig.work, "feat: new work")
    assert rig.push("feat").returncode == 0
    _git(rig.work, "checkout", "-q", "--detach")
    _commit(rig.work, "feat: detached and unpushed")
    gone = rig.push("--delete", "feat")
    assert gone.returncode == 0, _combined(gone)
    assert rig.remote_sha("refs/heads/feat") is None


# ------------------------------------------------------------------ scripts/run_with_deadline.py
def _deadline(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "run_with_deadline.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)


def test_a_command_past_its_deadline_is_stopped_with_exit_124():
    started = time.monotonic()
    proc = _deadline("0.02", "--", sys.executable, "-c", "import time; time.sleep(60)")
    assert proc.returncode == 124 and "deadline" in proc.stderr
    assert time.monotonic() - started < 30


def test_a_command_inside_its_deadline_keeps_its_own_exit_code_and_output():
    done = _deadline("5", "--", sys.executable, "-c", "print('hello'); raise SystemExit(3)")
    assert done.returncode == 3 and done.stdout.strip() == "hello"
    assert _deadline("5", "--", sys.executable, "-c", "pass").returncode == 0


def test_the_children_of_a_stopped_command_are_stopped_too(tmp_path: Path):
    marker = tmp_path / "grandchild-alive"
    child = tmp_path / "child.py"
    child.write_text(f"import pathlib, time\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text('x')\n",
                     encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(60)\n",
                      encoding="utf-8")
    assert _deadline("0.02", "--", sys.executable, str(parent)).returncode == 124
    time.sleep(4)
    assert not marker.exists()


def test_a_child_that_finishes_inside_the_deadline_is_left_alone(tmp_path: Path):
    marker = tmp_path / "child-done"
    child = tmp_path / "child.py"
    child.write_text(f"import pathlib, time\ntime.sleep(1)\npathlib.Path({str(marker)!r}).write_text('x')\n",
                     encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys\nsys.exit(subprocess.call([sys.executable, {str(child)!r}]))\n",
                      encoding="utf-8")
    assert _deadline("5", "--", sys.executable, str(parent)).returncode == 0
    assert marker.exists()


@pytest.mark.parametrize("args", [[], ["5"], ["5", "x"], ["0", "--", "true"], ["-1", "--", "true"], ["abc", "--", "true"],
                                  ["nan", "--", "true"], ["inf", "--", "true"]])
def test_deadline_usage_errors_are_exit_2(args: list[str]):
    assert deadline.main(args) == 2


def test_a_command_that_cannot_start_is_exit_127():
    assert deadline.main(["1", "--", "no-such-command-xyz"]) == 127


# ------------------------------------------------------------------ scripts/run_tests.sh
RUN_TESTS = ROOT / "scripts" / "run_tests.sh"
STUB = """#!/bin/sh
# Stand-in interpreter: logs every call (one argument per line), answers the preflight from environment variables.
for a in "$@"; do printf '%s\\n' "$a" >> "$STUB_LOG"; done
printf -- '--call--\\n' >> "$STUB_LOG"
case "$1" in
  *run_with_deadline.py) exec "$STUB_REAL_PYTHON" "$@" ;;
esac
case "$*" in
  "-m pip check")
    if [ -n "${STUB_PIP_PROBLEM:-}" ]; then echo "$STUB_PIP_PROBLEM"; exit 1; fi
    exit 0 ;;
  "-c import pytest") exit 0 ;;
  "-c import xdist") [ -n "${STUB_NO_XDIST:-}" ] && exit 1; exit 0 ;;
  "-m pytest"*)
    echo "pytest ran"
    [ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
    exit "${STUB_PYTEST_RC:-0}" ;;
esac
exit 99
"""


@pytest.fixture()
def stub(tmp_path: Path) -> tuple[Path, Path]:
    path = tmp_path / "stub bin" / "python"  # a space in the path: arrays must keep it in one piece
    path.parent.mkdir()
    path.write_text(STUB, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path, tmp_path / "stub.log"


def _run_tests(stub_files: tuple[Path, Path], *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    py, log = stub_files
    env = gates.clean_env()
    env.update(AEOS_GATES_PYTHON=str(py), STUB_LOG=str(log), STUB_REAL_PYTHON=sys.executable)
    env.pop("AEOS_TEST_DEADLINE_MIN", None)
    env.update(env_extra)
    return subprocess.run(["bash", str(RUN_TESTS), *args], env=env, capture_output=True, text=True, encoding="utf-8",
                          check=False, timeout=120)


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls: list[list[str]] = [[]]
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "--call--":
            calls.append([])
        else:
            calls[-1].append(line)
    return [c for c in calls if c]


@NEEDS_BASH
def test_a_missing_requirement_stops_before_pytest_and_names_it(stub: tuple[Path, Path]):
    proc = _run_tests(stub, STUB_PIP_PROBLEM="demo 1.0 requires fakepkg, which is not installed.")
    assert proc.returncode == 2
    assert "fakepkg" in proc.stderr and "pytest was not started" in proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "RUN_TESTS: FAIL rc=2"
    assert not [c for c in _calls(stub[1]) if c[:2] == ["-m", "pytest"]]  # pytest never started


@NEEDS_BASH
def test_a_missing_xdist_stops_before_pytest_and_names_the_package(stub: tuple[Path, Path]):
    proc = _run_tests(stub, STUB_NO_XDIST="1")
    assert proc.returncode == 2 and "pytest-xdist" in proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "RUN_TESTS: FAIL rc=2"
    assert not [c for c in _calls(stub[1]) if c[:2] == ["-m", "pytest"]]


@NEEDS_BASH
def test_a_complete_environment_runs_pytest_with_arguments_kept_whole(stub: tuple[Path, Path]):
    proc = _run_tests(stub, "--", "-k", "not slow and fast", "tests/unit/test a.py")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "RUN_TESTS: PASS rc=0"
    (pytest_call,) = [c for c in _calls(stub[1]) if c[:2] == ["-m", "pytest"]]
    assert pytest_call == ["-m", "pytest", "-q", "-m", "not integration and not network", "-n", "logical",
                           "-k", "not slow and fast", "tests/unit/test a.py"]


@NEEDS_BASH
def test_a_failing_pytest_is_reported_with_its_exit_code(stub: tuple[Path, Path]):
    proc = _run_tests(stub, STUB_PYTEST_RC="1")
    assert proc.returncode == 1 and proc.stdout.strip().splitlines()[-1] == "RUN_TESTS: FAIL rc=1"


@NEEDS_BASH
def test_the_deadline_stops_a_run_that_sleeps_past_it_and_a_generous_one_does_not(stub: tuple[Path, Path]):
    slow = _run_tests(stub, "--deadline", "0.02", STUB_SLEEP="60")
    assert slow.returncode == 124 and slow.stdout.strip().splitlines()[-1] == "RUN_TESTS: FAIL rc=124"
    fine = _run_tests(stub, "--deadline", "5", STUB_SLEEP="0")
    assert fine.returncode == 0 and fine.stdout.strip().splitlines()[-1] == "RUN_TESTS: PASS rc=0"
    bad = _run_tests(stub, "--deadline", "soon")
    assert bad.returncode == 2 and "positive number of minutes" in bad.stderr


@NEEDS_BASH
def test_a_bad_interpreter_override_is_an_error_with_the_marker(tmp_path: Path):
    env = gates.clean_env()
    env["AEOS_GATES_PYTHON"] = str(tmp_path / "missing" / "python")
    proc = subprocess.run(["bash", str(RUN_TESTS)], env=env, capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 2 and "AEOS_GATES_PYTHON" in proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "RUN_TESTS: FAIL rc=2"


def test_the_shell_scripts_follow_the_conventions():
    text = RUN_TESTS.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "\r" not in text
    assert "set -euo pipefail" in text and "RUN_TESTS: PASS" in text and "RUN_TESTS: FAIL" in text
    assert "timeout " not in text.replace("run_with_deadline", "")  # macOS has no timeout(1)
    try:
        index = subprocess.run(["git", "ls-files", "-s", "scripts/run_tests.sh"], cwd=ROOT, capture_output=True,
                               text=True, check=True, env=gates.clean_env()).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if index:
        assert index.startswith("100755 "), "mark it executable: git update-index --chmod=+x"


# ------------------------------------------------------------------ shellcheck in CI
SHELLCHECK_CMD = "shellcheck scripts/*.sh scripts/git-hooks/pre-push"


def test_ci_runs_shellcheck_in_an_existing_ubuntu_job_and_no_job_or_check_name_changed():
    jobs = yaml.safe_load(CI_TEXT)["jobs"]
    (host,) = [jid for jid, j in jobs.items() if any(s.get("run") == SHELLCHECK_CMD for s in j["steps"])]
    assert jobs[host]["runs-on"] == "ubuntu-latest" and jobs[host]["name"] == "commit subjects [G1]"
    assert (ROOT / "scripts" / "git-hooks" / "pre-push").is_file() and sorted((ROOT / "scripts").glob("*.sh"))
    base = subprocess.run(["git", "show", "origin/main:.github/workflows/ci.yml"], cwd=ROOT, capture_output=True,
                          text=True, check=False, env=gates.clean_env())
    if base.returncode == 0:
        before = yaml.safe_load(base.stdout)["jobs"]
        assert {jid: j["name"] for jid, j in jobs.items() if jid in before} == {jid: j["name"] for jid, j in before.items()}


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed on this host (CI runs it on ubuntu)")
def test_shellcheck_is_red_for_a_quoting_slip_and_green_for_the_quoted_script_and_the_repo_scripts(tmp_path: Path):
    def check(*files: Path) -> int:
        return subprocess.run(["shellcheck", *map(str, files)], capture_output=True, check=False).returncode

    bad = tmp_path / "bad.sh"
    bad.write_text('#!/usr/bin/env bash\nset -euo pipefail\nfile=$1\ncat $file\n', encoding="utf-8", newline="\n")
    good = tmp_path / "good.sh"
    good.write_text('#!/usr/bin/env bash\nset -euo pipefail\nfile=$1\ncat "$file"\n', encoding="utf-8", newline="\n")
    assert check(bad) != 0 and check(good) == 0
    repo_scripts = [*sorted((ROOT / "scripts").glob("*.sh")), ROOT / "scripts" / "git-hooks" / "pre-push"]
    assert check(*repo_scripts) == 0


# ------------------------------------------------------------------ docs and drift
def test_contributing_documents_the_guards_the_verdict_and_the_test_runner():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for needle in ("scripts/run_tests.sh", "RUN_TESTS: PASS", "NOT RUN:", "GATES: PASS|FAIL", "--report", "merge-conflict",
                   "git branch -f <branch> HEAD", "preflight.py --facts", "--reset-author", "shellcheck", "--deadline"):
        assert needle in text, needle
