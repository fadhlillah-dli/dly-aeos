"""CI verdict for ONE exact commit, from the GitHub check runs of that sha.

Incident this prevents: ad-hoc CI watcher scripts read an empty or still pending check rollup as green. This tool
never calls an unknown state green: an EMPTY list is PENDING, a required check that has not reported is PENDING,
and a check that was cancelled by a newer run is SUPERSEDED, not "fine".

  python scripts/ci_status.py --sha <40-hex>
  python scripts/ci_status.py --pr 21 [--expect-sha <sha or prefix>=7+] [--wait --deadline 30]
  python scripts/ci_status.py --after-merge 21 [--wait]       # judge the merge commit of PR 21 on main
  add: --repo owner/name  --require "<check name>" (extra required context)  --report out.json
       --required-only    judge from the required contexts only; other failures are listed and named, not RED

Data comes from the GitHub CLI (``gh api``), through a runner function the tests replace with fixture JSON:
  repos/{owner}/{repo}/commits/{sha}/check-runs   (paginated; the count is compared with total_count)
  repos/{owner}/{repo}/rulesets and each ruleset  (required status check contexts of active branch rulesets)
  repos/{owner}/{repo}/pulls/{n}                  (head sha / merge commit / base branch)
  repos/{owner}/{repo}/commits?sha=<base>&since=  (only for a RED --pr verdict: did the base branch move?)
  repos/{owner}/{repo}/actions/runs               (only to prove that a cancelled run was superseded)

Required checks = the required status check contexts of the active branch rulesets plus every ``--require``.
Every other reported check is informational: a failure of one still makes the verdict RED (default), a queued or
running one does not block. With ``--required-only`` the verdict comes from the required checks alone (GREEN when all
are success, skipped or neutral; PENDING while one is missing, queued or running; RED only when a required one
failed); a failed check that is not required is still listed, marked ``[NON-REQUIRED FAILED]``, and named in the
last line: ``CI: GREEN <sha> (non-required failed: <name>, ...)``. If no required context can be found, every
reported check counts as required (fail closed).
Only the check-run name is matched. Classic branch protection and commit-status contexts are not read (NOT-PROVEN).

Verdicts, strongest first: ERROR (api or auth failure), INFRA (see below), RED (a failure, timed out, action required, startup
failure or unrecognised conclusion on the LATEST attempt of a check), SUPERSEDED (a required check was cancelled
and a newer run exists for the same branch, or the PR head moved while waiting), PENDING (list empty, or a
required check is missing, queued or running), GREEN (every required check present and success, skipped or
neutral). A cancelled check with no newer run for its branch is RED, not SUPERSEDED.

INFRA: a required check that was cancelled because GitHub never gave the job a runner (its output or annotations say
the job was not acquired by a runner, as happens when hosted macOS capacity runs out while the job is queued). That
is infrastructure, not a code failure: re-run the job, do not read it as RED. It has its own exit code (4) and only
applies when no check failed and every cancelled required check has that reason.

``--wait`` polls (interval 30 s or more, ``--deadline`` minutes, default 30) until the verdict is not PENDING;
at the deadline the verdict is TIMEOUT. The last line is always exactly one ``CI: <VERDICT> <sha>``.
Exit 0 GREEN; 1 RED, SUPERSEDED or sha mismatch (MISMATCH); 2 TIMEOUT or ERROR; 3 PENDING without --wait; 4 INFRA.

HINT: when a ``--pr`` verdict is RED and the base branch of the pull request has commits newer than the earliest
check run of this commit, one line ``HINT: main moved since this run (<old>..<new>); update the branch, do not
re-run.`` is printed before the verdict line (never for GREEN or any other verdict). A re-run reuses the original
GITHUB_SHA and GITHUB_REF of the triggering event, so for ``pull_request`` it measures the old test merge commit
again; merging the base branch into the PR branch makes a new run. The check run start time stands in for the time
the test merge commit was made (NOT-PROVEN: a base commit landing between the trigger and the first job start is not
seen; a failed hint lookup prints a ``note:`` line and never changes the verdict). One extra read-only call.
Output is ASCII only. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

Runner = Callable[[list[str]], str]

MIN_INTERVAL = 30
GREEN, RED, PENDING, SUPERSEDED, ERROR, TIMEOUT, MISMATCH, INFRA = (
    "GREEN", "RED", "PENDING", "SUPERSEDED", "ERROR", "TIMEOUT", "MISMATCH", "INFRA")
EXIT_CODES = {GREEN: 0, RED: 1, SUPERSEDED: 1, MISMATCH: 1, TIMEOUT: 2, ERROR: 2, PENDING: 3, INFRA: 4}
# What GitHub writes on a job it could not start: "The job was not acquired by Runner of type hosted even after
# multiple attempts" (an annotation; the check-run output can carry it too).
NOT_ACQUIRED = re.compile(r"not\s+acquired\s+by\s+runner", re.I)

OK_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PREFIX_RE = re.compile(r"^[0-9a-f]{7,40}$")


class CiError(Exception):
    """API, auth or data failure: the verdict is ERROR."""


# ------------------------------------------------------------------------------------------------ gh access
def gh_runner(args: list[str]) -> str:
    """Run ``gh <args>`` and return stdout; any failure becomes CiError."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=180, check=False)
    except FileNotFoundError as exc:
        raise CiError("gh (GitHub CLI) is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CiError("gh timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[:300]
        raise CiError(f"gh {' '.join(args[:3])} failed: {detail}")
    return proc.stdout


def parse_pages(text: str) -> list[Any]:
    """``gh api --paginate`` prints one JSON document per page, back to back (objects or arrays)."""
    decoder = json.JSONDecoder()
    pos, pages = 0, []
    text = text.strip()
    while pos < len(text):
        try:
            value, end = decoder.raw_decode(text, pos)
        except ValueError as exc:
            raise CiError(f"gh returned invalid JSON: {exc}") from exc
        pages.append(value)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return pages


class Api:
    """Thin wrapper: builds repo-scoped paths and decodes (paginated) JSON."""

    def __init__(self, runner: Runner, repo: str | None) -> None:
        self.runner = runner
        # without --repo gh expands {owner}/{repo} from the current directory's remote itself
        self.base = f"repos/{repo}" if repo else "repos/{owner}/{repo}"
        self.base_ref = ""  # base branch of the pull request, set by resolve()

    def get(self, path: str, *, paginate: bool = False) -> list[Any]:
        args = ["api", "-H", "Accept: application/vnd.github+json"]
        if paginate:
            args.append("--paginate")
        args.append(f"{self.base}/{path}")
        return parse_pages(self.runner(args))

    def one(self, path: str) -> dict[str, Any]:
        pages = self.get(path)
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise CiError(f"unexpected response for {path}")
        return pages[0]


# ------------------------------------------------------------------------------------------------ data
def fetch_check_runs(api: Api, sha: str) -> list[dict[str, Any]]:
    pages = api.get(f"commits/{sha}/check-runs?per_page=100", paginate=True)
    runs: list[dict[str, Any]] = []
    total = None
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise CiError("check-runs response has no check_runs list")
        if total is None and isinstance(page.get("total_count"), int):
            total = page["total_count"]
        runs.extend(r for r in page["check_runs"] if isinstance(r, dict))
    unique = {r.get("id", id(r)) for r in runs}
    if total is not None and len(unique) < total:
        raise CiError(f"incomplete check-runs: got {len(unique)} of {total}")
    return runs


def required_contexts(api: Api) -> list[str]:
    """Required status check contexts of the active branch rulesets of the repository."""
    listed: list[Any] = []
    for page in api.get("rulesets?per_page=100", paginate=True):
        if not isinstance(page, list):
            raise CiError("rulesets response is not a list")
        listed.extend(page)
    contexts: list[str] = []
    for item in listed:
        if not isinstance(item, dict) or item.get("enforcement") != "active" or item.get("target") != "branch":
            continue
        detail = api.one(f"rulesets/{item.get('id')}")
        for rule in detail.get("rules") or []:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                ctx = check.get("context") if isinstance(check, dict) else None
                if isinstance(ctx, str) and ctx and ctx not in contexts:
                    contexts.append(ctx)
    return contexts


def latest_by_name(runs: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The newest attempt of every check name (re-runs share a name; ids only grow)."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name", ""))
        if not name:
            continue
        old = latest.get(name)
        if old is None or _order(run) >= _order(old):
            latest[name] = run
    return latest


def _order(run: dict[str, Any]) -> tuple[int, str]:
    rid = run.get("id")
    return (rid if isinstance(rid, int) else 0, str(run.get("started_at") or ""))


def cancellation_superseded(api: Api, sha: str) -> tuple[bool, str]:
    """True only when every cancelled workflow run of *sha* has a newer run (other sha) on the same branch."""
    pages = api.get(f"actions/runs?head_sha={sha}&per_page=100", paginate=True)
    mine = [r for p in pages if isinstance(p, dict) for r in p.get("workflow_runs", []) if isinstance(r, dict)]
    cancelled = [r for r in mine if r.get("conclusion") == "cancelled"]
    if not cancelled:
        return False, "no cancelled workflow run found for this sha"
    for run in cancelled:
        branch = run.get("head_branch")
        if not branch:
            return False, "cancelled run has no branch"
        other = api.get(f"actions/runs?branch={urllib.parse.quote(str(branch), safe='')}&per_page=100", paginate=True)
        newer = [r for p in other if isinstance(p, dict) for r in p.get("workflow_runs", [])
                 if isinstance(r, dict) and r.get("head_sha") != sha
                 and str(r.get("created_at") or "") > str(run.get("created_at") or "")]
        if not newer:
            return False, f"no newer run on branch {branch}"
    return True, "a newer run exists for the same branch"


def infra_cancelled(api: Api, run: dict[str, Any]) -> bool:
    """True when a cancelled check run says that no runner ever acquired its job (output text or annotations)."""
    output = run.get("output") if isinstance(run.get("output"), dict) else {}
    if any(NOT_ACQUIRED.search(str(output.get(key) or "")) for key in ("title", "summary", "text")):
        return True
    count = output.get("annotations_count")
    rid = run.get("id")
    if not isinstance(count, int) or count < 1 or not isinstance(rid, int):
        return False
    pages = api.get(f"check-runs/{rid}/annotations?per_page=100", paginate=True)
    return any(NOT_ACQUIRED.search(str(a.get("message") or "") + " " + str(a.get("title") or ""))
               for page in pages if isinstance(page, list) for a in page if isinstance(a, dict))


def base_moved_hint(api: Api, base_ref: str, runs: Sequence[dict[str, Any]]) -> str | None:
    """The HINT line when *base_ref* got commits after the earliest check run of the judged commit, else None."""
    started = sorted(str(r.get("started_at")) for r in runs if r.get("started_at"))
    if not started or not base_ref:
        return None
    pages = api.get(f"commits?sha={urllib.parse.quote(base_ref, safe='')}&since={started[0]}&per_page=100")
    newer = [c for page in pages if isinstance(page, list) for c in page if isinstance(c, dict)]
    if not newer or not SHA_RE.match(str(newer[0].get("sha") or "")):
        return None
    parents = newer[-1].get("parents")
    old = str(parents[0].get("sha") or "") if isinstance(parents, list) and parents and isinstance(parents[0], dict) else ""
    return (f"HINT: {base_ref} moved since this run ({old[:10] or 'unknown'}..{str(newer[0]['sha'])[:10]}); "
            "update the branch, do not re-run.")


# ------------------------------------------------------------------------------------------------ judgement
@dataclass
class Row:
    name: str
    required: bool
    state: str  # OK FAILED CANCELLED PENDING MISSING UNKNOWN
    detail: str = ""


@dataclass
class Judgement:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    required_source: str = ""
    required_only: bool = False
    non_required_failed: list[str] = field(default_factory=list)  # --required-only: named in the last line


def classify(run: dict[str, Any]) -> tuple[str, str]:
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    if status != "completed":
        return "PENDING", status or "unknown"
    if conclusion in OK_CONCLUSIONS:
        return "OK", conclusion
    if conclusion in FAILED_CONCLUSIONS:
        return "FAILED", conclusion
    if conclusion == "cancelled":
        return "CANCELLED", conclusion
    return "UNKNOWN", conclusion or "none"


def judge(runs: Sequence[dict[str, Any]], required: Sequence[str],
          superseded: Callable[[], tuple[bool, str]], required_only: bool = False,
          infra: Callable[[dict[str, Any]], bool] | None = None) -> Judgement:
    latest = latest_by_name(runs)
    source = "rulesets/--require" if required else "all reported checks (no required context found)"
    req = list(required) if required else sorted(latest)
    rows: list[Row] = []
    for name in req:
        run = latest.get(name)
        if run is None:
            rows.append(Row(name, True, "MISSING", "not reported yet"))
        else:
            state, detail = classify(run)
            rows.append(Row(name, True, state, detail))
    for name in sorted(set(latest) - set(req)):
        state, detail = classify(latest[name])
        rows.append(Row(name, False, state, detail))

    result = Judgement(PENDING, rows=rows, required_source=source, required_only=required_only)
    failed = [r for r in rows if r.state == "FAILED" or (r.required and r.state == "UNKNOWN")]
    if required_only:
        result.non_required_failed = [r.name for r in failed if not r.required]
        failed = [r for r in failed if r.required]
    cancelled = [r for r in rows if r.required and r.state == "CANCELLED"]
    infra_rows = [r for r in cancelled if infra is not None and infra(latest[r.name])]
    waiting = [r for r in rows if r.required and r.state in {"PENDING", "MISSING"}]
    if failed:
        result.verdict = RED
        result.reasons = [f"{r.name}: {r.detail}" for r in failed]
    elif cancelled and len(infra_rows) == len(cancelled):
        result.verdict = INFRA
        result.reasons = [f"{r.name}: cancelled, the job was not acquired by a runner" for r in infra_rows] + [
            "infrastructure, not a code failure: re-run the job (gh run rerun <run id> --failed); do not read it as RED"]
    elif cancelled:
        ok, why = superseded()
        result.verdict = SUPERSEDED if ok else RED
        result.reasons = [f"{r.name}: cancelled" for r in cancelled if r not in infra_rows] + [why]
    elif not latest:
        result.reasons = ["no check has reported for this commit yet (an empty list is never green)"]
    elif waiting:
        result.reasons = [f"{r.name}: {r.detail}" for r in waiting]
    else:
        result.verdict = GREEN
    return result


# ------------------------------------------------------------------------------------------------ run
def say(text: str, out: TextIO) -> None:
    out.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def resolve(api: Api, args: argparse.Namespace) -> tuple[str, str]:
    """(sha, description of where it came from)."""
    if args.sha:
        return args.sha, "--sha"
    number = args.pr if args.pr is not None else args.after_merge
    pr = api.one(f"pulls/{number}")
    if args.pr is not None:
        sha = str((pr.get("head") or {}).get("sha") or "").lower()
        api.base_ref = str((pr.get("base") or {}).get("ref") or "")
        label = f"PR #{number} head"
    else:
        if not pr.get("merged"):
            raise CiError(f"PR #{number} is not merged")
        if (pr.get("base") or {}).get("ref") != "main":
            raise CiError(f"PR #{number} was not merged into main")
        sha = str(pr.get("merge_commit_sha") or "").lower()
        label = f"merge commit of PR #{number}"
        if SHA_RE.match(sha):
            status = api.one(f"compare/{sha}...main").get("status")
            if status not in {"identical", "ahead"}:
                raise CiError(f"merge commit {sha[:10]} is not on main (compare status {status})")
    if not SHA_RE.match(sha):
        raise CiError(f"could not resolve a commit sha for {label}")
    return sha, label


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(argv: Sequence[str] | None = None, *, runner: Runner = gh_runner, sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.expect_sha and args.sha:
        parser.error("--expect-sha needs --pr or --after-merge (with --sha there is nothing to compare)")
    api = Api(runner, args.repo)
    sha = args.sha or "unknown"
    report: dict[str, Any] = {"verdict": ERROR, "sha": sha, "source": "", "polls": 0}

    def finish(verdict: str, shown_sha: str) -> int:
        report.update(verdict=verdict, sha=shown_sha, checked_at=datetime.now(UTC).isoformat(timespec="seconds"))
        if args.report:
            try:
                write_report(Path(args.report), report)
            except OSError as exc:
                say(f"error: cannot write --report {args.report}: {exc.__class__.__name__}", out)
                verdict = ERROR
        named = report.get("non_required_failed") or []
        suffix = f" (non-required failed: {', '.join(named)})" if named else ""
        say(f"CI: {verdict} {shown_sha}{suffix}", out)
        return EXIT_CODES[verdict]

    try:
        sha, source = resolve(api, args)
        report["source"] = source
        say(f"ci: judging {sha} ({source})", out)
        if args.expect_sha and not sha.startswith(args.expect_sha):
            say(f"mismatch: {source} is {sha} but --expect-sha is {args.expect_sha}", out)
            report["reasons"] = [f"expected {args.expect_sha}, found {sha}"]
            return finish(MISMATCH, sha)
        required = list(dict.fromkeys([*required_contexts(api), *args.require]))
        deadline = monotonic() + args.deadline * 60
        errors = 0
        while True:
            report["polls"] += 1
            result: Judgement | None
            try:
                if args.pr is not None and report["polls"] > 1:
                    head = str((api.one(f"pulls/{args.pr}").get("head") or {}).get("sha") or "").lower()
                    if head and head != sha:
                        say(f"PR #{args.pr} head moved to {head}; {sha} was superseded", out)
                        report["reasons"] = [f"PR head moved to {head}"]
                        return finish(SUPERSEDED, sha)
                check_runs = fetch_check_runs(api, sha)
                result = judge(check_runs, required, lambda: cancellation_superseded(api, sha),
                               args.required_only, lambda r: infra_cancelled(api, r))
                errors = 0
            except CiError as exc:
                errors += 1
                if not args.wait or errors >= 3:
                    raise
                say(f"transient error ({errors}/3), retrying: {exc}", out)
                result = None
            if result is not None:
                report.update(reasons=result.reasons, required_source=result.required_source,
                              required_only=result.required_only, non_required_failed=result.non_required_failed,
                              checks=[{"name": r.name, "required": r.required, "state": r.state, "detail": r.detail}
                                      for r in result.rows])
                if result.verdict != PENDING or not args.wait:
                    print_result(result, out)
                    if result.verdict == RED and args.pr is not None:
                        try:
                            hint = base_moved_hint(api, api.base_ref, check_runs)
                        except CiError as exc:
                            hint = f"note: could not check whether {api.base_ref} moved: {exc}"
                        if hint:
                            say(hint, out)
                    return finish(result.verdict, sha)
                say(f"pending ({len([r for r in result.rows if r.required and r.state in {'PENDING', 'MISSING'}])} "
                    "required not done yet)", out)
            remaining = deadline - monotonic()
            if remaining <= 0:
                say(f"deadline of {args.deadline} minutes reached while still pending", out)
                return finish(TIMEOUT, sha)
            if remaining < args.interval:
                # never poll again sooner than the interval: use up the time left and give up
                sleep(remaining)
                say(f"deadline of {args.deadline} minutes reached while still pending", out)
                return finish(TIMEOUT, sha)
            sleep(args.interval)
    except CiError as exc:
        say(f"error: {exc}", out)
        report["reasons"] = [str(exc)]
        return finish(ERROR, sha)


def print_result(result: Judgement, out: TextIO) -> None:
    required = [r for r in result.rows if r.required]
    other = [r for r in result.rows if not r.required]
    say(f"required checks ({len(required)}, from {result.required_source}):", out)
    for row in required:
        say(f"  [{row.state}] {row.name} ({row.detail})", out)
    say(f"other checks ({len(other)}, informational):", out)
    for row in other:
        marker = " [NON-REQUIRED FAILED]" if result.required_only and row.name in result.non_required_failed else ""
        say(f"  [{row.state}] {row.name} ({row.detail}){marker}", out)
    for reason in result.reasons:
        say(f"reason: {reason}", out)


def sha_arg(value: str) -> str:
    value = value.strip().lower()
    if not SHA_RE.match(value):
        raise argparse.ArgumentTypeError("--sha must be the full 40-hex commit sha")
    return value


def prefix_arg(value: str) -> str:
    value = value.strip().lower()
    if not PREFIX_RE.match(value):
        raise argparse.ArgumentTypeError("--expect-sha must be 7 to 40 hex characters")
    return value


def interval_arg(value: str) -> int:
    number = int(value)
    if number < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"--interval must be at least {MIN_INTERVAL} seconds")
    return number


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ci_status.py", description="CI verdict for one exact commit.")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--sha", type=sha_arg, help="full commit sha to judge")
    what.add_argument("--pr", type=int, help="judge the current head of this pull request")
    what.add_argument("--after-merge", type=int, metavar="PR", help="judge the merge commit of this PR on main")
    p.add_argument("--expect-sha", type=prefix_arg, default=None,
                   help="with --pr / --after-merge: exit 1 when the resolved commit does not start with this")
    p.add_argument("--repo", default=None, help="owner/name (default: the current directory's GitHub remote)")
    p.add_argument("--require", action="append", default=[], metavar="NAME", help="extra required check name")
    p.add_argument("--required-only", action="store_true",
                   help="verdict from the required checks only; other failures are listed and named, not RED")
    p.add_argument("--wait", action="store_true", help="poll while PENDING")
    p.add_argument("--deadline", type=float, default=30.0, help="minutes to wait (default 30)")
    p.add_argument("--interval", type=interval_arg, default=MIN_INTERVAL, help="seconds between polls (min 30)")
    p.add_argument("--report", default=None, metavar="PATH.json", help="write the full result as JSON")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
