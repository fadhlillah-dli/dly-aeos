"""scripts/ci_status.py: one verdict for one exact commit, from fixture JSON (no network, no gh).

Incident class: a watcher read an empty or pending check rollup as green. Every verdict below has a violating
fixture and a near-miss legitimate one (an empty list is PENDING while a full green list is GREEN, a failed
OLD attempt that was re-run green is GREEN, a failed NEW attempt after a green one is RED, ...)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_ci_status", ROOT / "scripts" / "ci_status.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ci = _load()

SHA = "a" * 40
OTHER = "b" * 40
REPO = "octo/demo"
BASE = f"repos/{REPO}"
REQUIRED = ["backend [G1,G2] (windows-latest)", "frontend [G1,G3]"]


def check(name, status="completed", conclusion="success", rid=1, started="2026-01-01T00:00:00Z"):
    return {"id": rid, "name": name, "status": status, "conclusion": conclusion, "started_at": started}


def ruleset_files(contexts, enforcement="active", target="branch"):
    rules = [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": c, "integration_id": 15368} for c in contexts]}}]
    return {f"{BASE}/rulesets": [{"id": 7, "enforcement": enforcement, "target": target}],
            f"{BASE}/rulesets/7": {"id": 7, "rules": rules}}


class FakeGh:
    """Runner replacement: maps the request path (without query) to a JSON value; a list value is a sequence of
    responses (the last one repeats), which lets a test change the answer between polls."""

    def __init__(self, files, fail=None):
        self.files = dict(files)
        self.calls = []
        self.fail = fail or {}

    def __call__(self, args):
        path = args[-1].split("?")[0]
        self.calls.append(path)
        if path in self.fail:
            raise ci.CiError(self.fail[path])
        if path not in self.files:
            raise ci.CiError(f"gh api {path} failed: HTTP 404")
        value = self.files[path]
        if isinstance(value, _Seq):
            value = value.next()
        return value if isinstance(value, str) else json.dumps(value)


class _Seq:
    def __init__(self, *items):
        self.items = list(items)

    def next(self):
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


def runs_file(runs, sha=SHA, total=None):
    return {f"{BASE}/commits/{sha}/check-runs": {"total_count": len(runs) if total is None else total,
                                                  "check_runs": runs}}


def files(runs, required=REQUIRED, sha=SHA, **extra):
    out = {**ruleset_files(required), **runs_file(runs, sha)}
    out.update(extra)
    return out


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def call(argv, gh, clock=None):
    clock = clock or Clock()
    buf = io.StringIO()
    code = ci.run(["--repo", REPO, *argv], runner=gh, sleep=clock.sleep, monotonic=clock.monotonic, out=buf)
    text = buf.getvalue()
    text.encode("ascii")
    lines = text.strip().splitlines()
    assert sum(1 for line in lines if line.startswith("CI: ")) == 1, "exactly one verdict line"
    assert lines[-1].startswith("CI: ")
    return code, lines[-1], text


def all_green():
    return [check(n, rid=i + 1) for i, n in enumerate([*REQUIRED, "supply-chain [NO-GATE]"])]


# ------------------------------------------------------------------------------------------------ verdicts
def test_green_when_every_required_check_succeeded_positive_control():
    code, last, text = call(["--sha", SHA], FakeGh(files(all_green())))
    assert (code, last) == (0, f"CI: GREEN {SHA}")
    assert "required checks (2, from rulesets/--require)" in text


def test_green_accepts_skipped_and_neutral_as_passing():
    runs = [check(REQUIRED[0], conclusion="skipped"), check(REQUIRED[1], conclusion="neutral", rid=2)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[0] == 0


def test_empty_check_list_is_pending_never_green():
    code, last, text = call(["--sha", SHA], FakeGh(files([])))
    assert (code, last) == (3, f"CI: PENDING {SHA}")
    assert "empty list is never green" in text


def test_empty_list_with_no_required_context_found_is_still_pending():
    code, last, _ = call(["--sha", SHA], FakeGh(files([], required=[])))
    assert (code, last) == (3, f"CI: PENDING {SHA}")


def test_queued_required_check_is_pending():
    runs = [check(REQUIRED[0]), check(REQUIRED[1], status="queued", conclusion=None, rid=2)]
    code, last, _ = call(["--sha", SHA], FakeGh(files(runs)))
    assert (code, last) == (3, f"CI: PENDING {SHA}")


def test_in_progress_required_check_is_pending():
    runs = [check(REQUIRED[0], status="in_progress", conclusion=None), check(REQUIRED[1], rid=2)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[0] == 3


def test_all_success_but_a_required_context_missing_is_pending():
    runs = [check(REQUIRED[0]), check("supply-chain [NO-GATE]", rid=2)]
    code, last, text = call(["--sha", SHA], FakeGh(files(runs)))
    assert (code, last) == (3, f"CI: PENDING {SHA}")
    assert "[MISSING] frontend [G1,G3]" in text


def test_one_failure_is_red():
    runs = [check(REQUIRED[0], conclusion="failure"), check(REQUIRED[1], rid=2)]
    code, last, text = call(["--sha", SHA], FakeGh(files(runs)))
    assert (code, last) == (1, f"CI: RED {SHA}")
    assert "failure" in text


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required", "startup_failure", "stale", "weird"])
def test_red_conclusions_including_unrecognised_ones_fail_closed(conclusion):
    runs = [check(REQUIRED[0], conclusion=conclusion), check(REQUIRED[1], rid=2)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[0] == 1


def test_failure_wins_over_pending():
    runs = [check(REQUIRED[0], conclusion="failure"), check(REQUIRED[1], status="queued", conclusion=None, rid=2)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[1].startswith("CI: RED")


def test_failed_informational_check_is_red_but_running_informational_check_is_not_blocking():
    runs = [*all_green()[:2], check("lint", conclusion="failure", rid=9)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[1].startswith("CI: RED")
    runs = [*all_green()[:2], check("supply-chain [NO-GATE]", status="in_progress", conclusion=None, rid=9)]
    code, last, text = call(["--sha", SHA], FakeGh(files(runs)))
    assert code == 0 and last.startswith("CI: GREEN")
    assert "other checks (1, informational)" in text


def test_latest_attempt_wins_a_rerun_after_failure_is_green_and_a_new_failure_after_success_is_red():
    reran = [check(REQUIRED[0], conclusion="failure", rid=1), check(REQUIRED[0], rid=5), check(REQUIRED[1], rid=2)]
    assert call(["--sha", SHA], FakeGh(files(reran)))[0] == 0
    broke = [check(REQUIRED[0], rid=1), check(REQUIRED[0], conclusion="failure", rid=5), check(REQUIRED[1], rid=2)]
    assert call(["--sha", SHA], FakeGh(files(broke)))[0] == 1


def _cancelled_files(newer_run: bool):
    runs = [check(REQUIRED[0], conclusion="cancelled"), check(REQUIRED[1], conclusion="cancelled", rid=2)]
    mine = {"id": 1, "head_sha": SHA, "head_branch": "feat/x", "conclusion": "cancelled",
            "created_at": "2026-01-01T10:00:00Z"}
    branch_runs = [mine]
    if newer_run:
        branch_runs.append({"id": 2, "head_sha": OTHER, "head_branch": "feat/x", "conclusion": None,
                            "created_at": "2026-01-01T10:05:00Z"})
    return files(runs, **{f"{BASE}/actions/runs": _Seq({"workflow_runs": [mine]}, {"workflow_runs": branch_runs})})


def test_cancelled_because_a_newer_run_exists_is_superseded():
    code, last, text = call(["--sha", SHA], FakeGh(_cancelled_files(True)))
    assert (code, last) == (1, f"CI: SUPERSEDED {SHA}")
    assert "newer run exists" in text


def test_cancelled_without_a_newer_run_is_red_not_superseded():
    code, last, text = call(["--sha", SHA], FakeGh(_cancelled_files(False)))
    assert (code, last) == (1, f"CI: RED {SHA}")
    assert "no newer run" in text


def test_cancelled_informational_check_does_not_make_green_red():
    runs = [*all_green()[:2], check("extra", conclusion="cancelled", rid=9)]
    assert call(["--sha", SHA], FakeGh(files(runs)))[0] == 0


def test_no_required_context_found_judges_every_reported_check_and_never_defaults_to_green():
    green = call(["--sha", SHA], FakeGh(files(all_green(), required=[])))
    assert green[0] == 0 and "all reported checks" in green[2]
    running = [*all_green()[:2], check("extra", status="in_progress", conclusion=None, rid=9)]
    assert call(["--sha", SHA], FakeGh(files(running, required=[])))[0] == 3


def test_inactive_or_non_branch_rulesets_do_not_contribute_required_contexts():
    for kw in ({"enforcement": "evaluate"}, {"target": "tag"}):
        data = {**files(all_green()[:1], required=[]), **ruleset_files(["never-reported"], **kw)}
        assert call(["--sha", SHA], FakeGh(data))[0] == 0, kw


def test_extra_require_option_adds_a_required_context():
    data = files(all_green()[:2], required=REQUIRED)
    assert call(["--sha", SHA], FakeGh(data))[0] == 0
    assert call(["--sha", SHA, "--require", "deploy-preview"], FakeGh(data))[0] == 3


# ------------------------------------------------------------------------------------------------ api failures
def test_api_failure_is_error_exit_2():
    gh = FakeGh(files(all_green()), fail={f"{BASE}/commits/{SHA}/check-runs": "gh api failed: HTTP 401 bad credentials"})
    code, last, text = call(["--sha", SHA], gh)
    assert (code, last) == (2, f"CI: ERROR {SHA}") and "401" in text


def test_rulesets_unreadable_is_error_not_green():
    gh = FakeGh(files(all_green()), fail={f"{BASE}/rulesets": "HTTP 403"})
    assert call(["--sha", SHA], gh)[0:2] == (2, f"CI: ERROR {SHA}")


def test_truncated_check_runs_listing_is_error():
    data = files([check(REQUIRED[0])], **runs_file([check(REQUIRED[0])], total=5))
    assert call(["--sha", SHA], FakeGh(data))[0] == 2


def test_missing_gh_binary_is_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ci.subprocess, "run", boom)
    buf = io.StringIO()
    assert ci.run(["--repo", REPO, "--sha", SHA], out=buf) == 2
    assert buf.getvalue().strip().splitlines()[-1] == f"CI: ERROR {SHA}"


def test_paginated_concatenated_json_pages_are_merged():
    page1 = json.dumps({"total_count": 2, "check_runs": [check(REQUIRED[0])]})
    page2 = json.dumps({"total_count": 2, "check_runs": [check(REQUIRED[1], rid=2)]})
    data = {**ruleset_files(REQUIRED), f"{BASE}/commits/{SHA}/check-runs": page1 + "\n" + page2}
    assert call(["--sha", SHA], FakeGh(data))[0] == 0


# ------------------------------------------------------------------------------------------------ pr / sha
def pr_file(head, number=21):
    return {f"{BASE}/pulls/{number}": {"head": {"sha": head}, "merged": False, "base": {"ref": "main"}}}


def test_pr_resolves_head_sha_and_judges_it():
    code, last, text = call(["--pr", "21"], FakeGh({**files(all_green()), **pr_file(SHA)}))
    assert (code, last) == (0, f"CI: GREEN {SHA}") and "PR #21 head" in text


def test_expect_sha_mismatch_exits_1_without_reading_checks():
    gh = FakeGh({**files(all_green()), **pr_file(OTHER)})
    code, last, text = call(["--pr", "21", "--expect-sha", SHA[:12]], gh)
    assert (code, last) == (1, f"CI: MISMATCH {OTHER}")
    assert "mismatch" in text and not any("check-runs" in c for c in gh.calls)


def test_expect_sha_match_by_prefix_is_judged_normally():
    gh = FakeGh({**files(all_green()), **pr_file(SHA)})
    assert call(["--pr", "21", "--expect-sha", SHA[:7]], gh)[0] == 0


def test_expect_sha_with_plain_sha_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        ci.run(["--repo", REPO, "--sha", SHA, "--expect-sha", SHA], runner=FakeGh({}), out=io.StringIO())
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["abc1234", "z" * 40, "a" * 41, ""])
def test_sha_must_be_the_full_hex_sha(bad):
    with pytest.raises(SystemExit) as exc:
        ci.run(["--repo", REPO, "--sha", bad], runner=FakeGh({}), out=io.StringIO())
    assert exc.value.code == 2


def test_after_merge_judges_the_merge_commit_on_main():
    pr = {"head": {"sha": OTHER}, "merged": True, "base": {"ref": "main"}, "merge_commit_sha": SHA}
    data = {**files(all_green()), f"{BASE}/pulls/21": pr, f"{BASE}/compare/{SHA}...main": {"status": "ahead"}}
    code, last, text = call(["--after-merge", "21"], FakeGh(data))
    assert (code, last) == (0, f"CI: GREEN {SHA}") and "merge commit of PR #21" in text


@pytest.mark.parametrize("pr, compare", [
    ({"merged": False, "base": {"ref": "main"}, "merge_commit_sha": SHA}, "ahead"),
    ({"merged": True, "base": {"ref": "release"}, "merge_commit_sha": SHA}, "ahead"),
    ({"merged": True, "base": {"ref": "main"}, "merge_commit_sha": SHA}, "diverged"),
    ({"merged": True, "base": {"ref": "main"}, "merge_commit_sha": None}, "ahead"),
])
def test_after_merge_refuses_unmerged_other_branch_or_not_on_main(pr, compare):
    data = {**files(all_green()), f"{BASE}/pulls/21": pr, f"{BASE}/compare/{SHA}...main": {"status": compare}}
    assert call(["--after-merge", "21"], FakeGh(data))[0] == 2


# ------------------------------------------------------------------------------------------------ wait
def test_wait_polls_until_green_with_at_least_30_seconds_between_polls():
    pending = [check(REQUIRED[0], status="in_progress", conclusion=None), check(REQUIRED[1], rid=2)]
    data = files(pending)
    data[f"{BASE}/commits/{SHA}/check-runs"] = _Seq(
        {"total_count": 2, "check_runs": pending}, {"total_count": 2, "check_runs": pending},
        {"total_count": 2, "check_runs": all_green()[:2]})
    clock = Clock()
    code, last, _ = call(["--sha", SHA, "--wait", "--deadline", "10"], FakeGh(data), clock)
    assert (code, last) == (0, f"CI: GREEN {SHA}")
    assert clock.sleeps == [30, 30]


def test_deadline_exits_2_timeout_while_still_pending():
    clock = Clock()
    code, last, text = call(["--sha", SHA, "--wait", "--deadline", "2"], FakeGh(files([])), clock)
    assert (code, last) == (2, f"CI: TIMEOUT {SHA}")
    assert sum(clock.sleeps) == pytest.approx(120) and all(s <= 30 for s in clock.sleeps)
    assert "deadline" in text


def test_last_partial_interval_never_causes_a_poll_closer_than_30_seconds_to_the_previous_one():
    clock = Clock()
    polled_at = []

    class TimedGh(FakeGh):
        def __call__(self, args):
            if args[-1].split("?")[0].endswith("/check-runs"):
                polled_at.append(clock.now)
            return super().__call__(args)

    # 1.25 minutes = 75 s: polls at 0 s and 30 s and 60 s, then 15 s are left, which is less than one interval
    code, last, _ = call(["--sha", SHA, "--wait", "--deadline", "1.25"], TimedGh(files([])), clock)
    assert (code, last) == (2, f"CI: TIMEOUT {SHA}")
    assert len(polled_at) == 3 and clock.sleeps == [30, 30, 15.0]
    assert all(b - a >= 30 for a, b in zip(polled_at, polled_at[1:], strict=False))
    # near miss: a deadline that is an exact multiple of the interval still polls at the very end
    clock = Clock()
    polled_at.clear()
    call(["--sha", SHA, "--wait", "--deadline", "1"], TimedGh(files([])), clock)
    assert len(polled_at) == 3 and clock.sleeps == [30, 30]


def test_pending_without_wait_exits_3_and_does_not_sleep():
    clock = Clock()
    assert call(["--sha", SHA], FakeGh(files([])), clock)[0] == 3
    assert clock.sleeps == []


def test_wait_stops_at_a_red_verdict_immediately():
    clock = Clock()
    runs = [check(REQUIRED[0], conclusion="failure"), check(REQUIRED[1], status="queued", conclusion=None, rid=2)]
    assert call(["--sha", SHA, "--wait"], FakeGh(files(runs)), clock)[0] == 1
    assert clock.sleeps == []


def test_pr_head_moving_while_waiting_is_superseded():
    data = {**files([]), f"{BASE}/pulls/21": _Seq({"head": {"sha": SHA}}, {"head": {"sha": OTHER}})}
    code, last, text = call(["--pr", "21", "--wait", "--deadline", "10"], FakeGh(data))
    assert (code, last) == (1, f"CI: SUPERSEDED {SHA}") and "head moved" in text


def test_transient_api_error_while_waiting_is_retried_but_three_in_a_row_are_fatal():
    ok = {"total_count": 2, "check_runs": all_green()[:2]}
    data = files(all_green())
    data[f"{BASE}/commits/{SHA}/check-runs"] = _Seq("not json{", ok)
    assert call(["--sha", SHA, "--wait"], FakeGh(data))[0] == 0
    data[f"{BASE}/commits/{SHA}/check-runs"] = "not json{"
    assert call(["--sha", SHA, "--wait", "--deadline", "30"], FakeGh(data))[0:2] == (2, f"CI: ERROR {SHA}")


@pytest.mark.parametrize("seconds", ["0", "29", "-5"])
def test_poll_interval_below_30_seconds_is_refused(seconds):
    with pytest.raises(SystemExit) as exc:
        ci.run(["--repo", REPO, "--sha", SHA, "--wait", "--interval", seconds], runner=FakeGh({}), out=io.StringIO())
    assert exc.value.code == 2


# ------------------------------------------------------------------------------------------------ report
def test_report_json_is_written_for_green_and_for_error(tmp_path):
    path = tmp_path / "out" / "r.json"
    path.parent.mkdir()
    call(["--sha", SHA, "--report", str(path)], FakeGh(files(all_green())))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "GREEN" and data["sha"] == SHA and data["polls"] == 1
    assert {c["name"] for c in data["checks"]} >= set(REQUIRED) and data["checks"][0]["state"] == "OK"
    call(["--sha", SHA, "--report", str(path)], FakeGh({}))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"] == "ERROR" and data["reasons"]


def test_unwritable_report_path_still_prints_one_error_verdict_and_exits_2(tmp_path):
    bad = tmp_path / "no-such-dir" / "r.json"
    code, last, text = call(["--sha", SHA, "--report", str(bad)], FakeGh(files(all_green())))
    assert (code, last) == (2, f"CI: ERROR {SHA}")
    assert "cannot write --report" in text and not bad.exists()
    # near miss: the same run with a writable path stays GREEN
    good = tmp_path / "r.json"
    assert call(["--sha", SHA, "--report", str(good)], FakeGh(files(all_green())))[0:2] == (0, f"CI: GREEN {SHA}")


def test_without_repo_option_gh_expands_the_owner_repo_placeholders_itself():
    seen = []

    def runner(args):
        seen.append(args[-1])
        raise ci.CiError("stop")

    ci.run(["--sha", SHA], runner=runner, out=io.StringIO())
    assert seen[0].startswith("repos/{owner}/{repo}/")


def test_non_ascii_check_names_are_escaped_in_the_output():
    runs = [check("bäckend", conclusion="failure")]
    code, _, text = call(["--sha", SHA], FakeGh(files(runs, required=[])))
    assert code == 1 and "b\\xe4ckend" in text


# ------------------------------------------------------------------------------------------------ --required-only
def _one_non_required_failure(required_status="success"):
    """Both required checks reported (or one still running) and a check that is not required has failed: the PR #21
    situation, where only 'commit subjects [G1]' failed."""
    return [check(REQUIRED[0]), check(REQUIRED[1], status="in_progress" if required_status == "running" else "completed",
                                      conclusion=None if required_status == "running" else required_status, rid=2),
            check("commit subjects [G1]", conclusion="failure", rid=9)]


def test_a_non_required_failure_is_red_by_default_and_green_with_required_only():
    gh = FakeGh(files(_one_non_required_failure()))
    assert call(["--sha", SHA], gh)[:2] == (1, f"CI: RED {SHA}")  # the default is unchanged
    code, last, text = call(["--sha", SHA, "--required-only"], FakeGh(files(_one_non_required_failure())))
    assert (code, last) == (0, f"CI: GREEN {SHA} (non-required failed: commit subjects [G1])")
    assert "[FAILED] commit subjects [G1] (failure) [NON-REQUIRED FAILED]" in text


def test_required_only_names_every_non_required_failure_in_the_last_line():
    runs = [*_one_non_required_failure(), check("lint", conclusion="timed_out", rid=10)]
    last = call(["--sha", SHA, "--required-only"], FakeGh(files(runs)))[1]
    assert last == f"CI: GREEN {SHA} (non-required failed: commit subjects [G1], lint)"


def test_required_only_with_no_failure_has_a_plain_last_line():
    code, last, text = call(["--sha", SHA, "--required-only"], FakeGh(files(all_green())))
    assert (code, last) == (0, f"CI: GREEN {SHA}") and "NON-REQUIRED" not in text


@pytest.mark.parametrize("flags", [[], ["--required-only"]])
def test_a_required_failure_is_red_either_way(flags):
    runs = [check(REQUIRED[0], conclusion="failure"), check(REQUIRED[1], rid=2),
            check("commit subjects [G1]", conclusion="failure", rid=9)]
    code, last, _ = call(["--sha", SHA, *flags], FakeGh(files(runs)))
    assert code == 1 and last.startswith(f"CI: RED {SHA}")


def test_required_only_with_a_required_check_still_running_is_pending_and_names_the_non_required_failure():
    runs = _one_non_required_failure("running")
    code, last, _ = call(["--sha", SHA, "--required-only"], FakeGh(files(runs)))
    assert (code, last) == (3, f"CI: PENDING {SHA} (non-required failed: commit subjects [G1])")
    assert call(["--sha", SHA], FakeGh(files(runs)))[1] == f"CI: RED {SHA}"  # near miss: the default stays RED


def test_required_only_with_a_missing_required_check_is_pending():
    runs = [check(REQUIRED[0]), check("commit subjects [G1]", conclusion="failure", rid=9)]
    assert call(["--sha", SHA, "--required-only"], FakeGh(files(runs)))[0] == 3


def test_required_only_without_any_required_context_judges_every_check_as_required():
    code, last, _ = call(["--sha", SHA, "--required-only"], FakeGh(files(_one_non_required_failure(), required=[])))
    assert code == 1 and last == f"CI: RED {SHA}"  # fail closed: nothing is "non-required" when none is known


def test_required_only_is_in_the_report(tmp_path):
    report = tmp_path / "r.json"
    call(["--sha", SHA, "--required-only", "--report", str(report)], FakeGh(files(_one_non_required_failure())))
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["required_only"] is True and data["non_required_failed"] == ["commit subjects [G1]"]


# ------------------------------------------------------------------------------------------------ INFRA
NOT_ACQUIRED_MESSAGE = "The job was not acquired by Runner of type hosted even after multiple attempts"


def _infra_files(via="annotations", second="success"):
    """macOS arm64 capacity ran out: a required job was cancelled before any runner took it."""
    cancelled = check(REQUIRED[0], conclusion="cancelled")
    extra = {}
    if via == "annotations":
        cancelled["output"] = {"title": REQUIRED[0], "summary": None, "annotations_count": 1}
        extra[f"{BASE}/check-runs/1/annotations"] = [{"message": NOT_ACQUIRED_MESSAGE, "annotation_level": "failure"}]
    elif via == "output":
        cancelled["output"] = {"title": REQUIRED[0], "summary": NOT_ACQUIRED_MESSAGE, "annotations_count": 0}
    elif via == "other-annotation":
        cancelled["output"] = {"title": REQUIRED[0], "summary": None, "annotations_count": 1}
        extra[f"{BASE}/check-runs/1/annotations"] = [{"message": "The operation was canceled.", "annotation_level": "failure"}]
    other = check(REQUIRED[1], conclusion=second, rid=2)
    return files([cancelled, other], **extra, **{f"{BASE}/actions/runs": {"workflow_runs": []}})


@pytest.mark.parametrize("via", ["annotations", "output"])
def test_a_job_that_no_runner_acquired_is_infra_with_its_own_exit_code(via):
    code, last, text = call(["--sha", SHA], FakeGh(_infra_files(via)))
    assert (code, last) == (4, f"CI: INFRA {SHA}")
    assert "not acquired by a runner" in text and "re-run" in text


def test_a_plain_cancellation_is_not_infra():
    """Near miss: the same cancelled job with another annotation, or none, stays RED (or SUPERSEDED), never INFRA."""
    code, last, _ = call(["--sha", SHA], FakeGh(_infra_files("other-annotation")))
    assert (code, last) == (1, f"CI: RED {SHA}")
    code, last, _ = call(["--sha", SHA], FakeGh(_infra_files("none")))
    assert (code, last) == (1, f"CI: RED {SHA}")


def test_infra_never_hides_a_real_failure_or_a_second_plain_cancellation():
    assert call(["--sha", SHA], FakeGh(_infra_files("annotations", second="failure")))[1] == f"CI: RED {SHA}"
    runs = [check(REQUIRED[0], conclusion="cancelled", rid=1), check(REQUIRED[1], conclusion="cancelled", rid=2)]
    runs[0]["output"] = {"summary": NOT_ACQUIRED_MESSAGE}  # only one of the two is infrastructure
    gh = FakeGh(files(runs, **{f"{BASE}/actions/runs": {"workflow_runs": []}}))
    assert call(["--sha", SHA], gh)[1] == f"CI: RED {SHA}"


def test_infra_on_a_check_that_is_not_required_is_ignored():
    runs = [*all_green()[:2], check("extra", conclusion="cancelled", rid=9)]
    runs[2]["output"] = {"summary": NOT_ACQUIRED_MESSAGE}
    assert call(["--sha", SHA], FakeGh(files(runs)))[0] == 0


def test_the_infra_verdict_is_in_the_docstring_and_the_exit_code_differs_from_red():
    assert ci.EXIT_CODES[ci.INFRA] not in {ci.EXIT_CODES[ci.RED], ci.EXIT_CODES[ci.ERROR], ci.EXIT_CODES[ci.PENDING]}
    assert "INFRA" in ci.__doc__
