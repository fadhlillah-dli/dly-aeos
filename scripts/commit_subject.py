"""Commit-message gate (CONTRIBUTING.md, "Rules for every change"): the first line is a Conventional Commits subject
`type(scope)?!?: description`, and no commit carries an AI attribution in its message, author or committer. The
attribution rules are the repository's own guard, backend/aeos/core/attribution.py (AGENTS.md invariant 11,
ADR-0121), not a copy of them. Stdlib only, ASCII-only output.

    python scripts/commit_subject.py <base>..<head>      exit 0 ok, 1 violations, 2 usage or git error

Merge commits (two or more parents) are exempt from the subject format but not from the attribution check (GitHub
writes their message). Commits at or below BASELINE are never checked. `Revert "<inner>"` and `Reapply "<inner>"`
(as `git revert` writes them) pass when the inner subject passes or is a merge subject (`git revert -m 1` of a merge
commit quotes `Merge pull request #7 from ...`)."""

from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from aeos.core.attribution import ai_identity_problem, find_ai_attribution  # noqa: E402

TYPES = ("feat", "fix", "docs", "test", "ci", "build", "chore", "refactor", "perf", "style", "revert", "security")
MAX_LENGTH = 100
PATTERN = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([^()\s]+\))?!?: \S.*$")
REVERT = re.compile(r'^(?:Revert|Reapply) "(.+)"$')
MERGE_SUBJECT = re.compile(r"^Merge (?:pull request #\d+ from |branch |remote-tracking branch )")

# Nothing at or below this commit is checked, so the gate is green on the day it landed. It is origin/main as of
# 2026-10-26 (merge of PR #16). Of the 186 non-merge commits up to it, 30 have a subject over 100 characters and 1
# has a scope list ("ci,docs:") that Conventional Commits does not allow; rewriting a protected main is not an option.
# Only commits after it must comply. Moving this constant later only widens what is exempt: do it with the owner.
BASELINE = "bd54f897198fae6236d9143a5db00cddff923fd2"

# git forbids NUL in a commit message, so it is the one separator a message cannot contain (`git log -z`).
_NUL = "\0"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_FIELDS = 7  # %H %an %ae %cn %ce %P %B
MAX_FINDINGS_SHOWN = 3


def ascii_text(text: str) -> str:
    """Printable ASCII only (legacy Windows consoles): anything else becomes a \\uXXXX escape, so nothing is lost."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def check_subject(subject: str) -> str | None:
    """None when the subject complies, else the reason. `Revert "<inner>"` and `Reapply "<inner>"` comply when the
    inner subject does or is a merge subject (the wrapper is exempt from the length limit, the inner subject is not)."""
    wrapped = REVERT.match(subject)
    if wrapped:
        inner = wrapped.group(1)
        if MERGE_SUBJECT.match(inner):
            return None
        reason = check_subject(inner)
        return None if reason is None else f"reverted subject does not comply: {reason}"
    if len(subject) > MAX_LENGTH:
        return f"first line is {len(subject)} characters (maximum {MAX_LENGTH})"
    if not PATTERN.match(subject):
        return "expected 'type(scope)?!?: description' with type " + "|".join(TYPES)
    return None


def _git(args: list[str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, check=False)


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    subject: str  # first line of the message
    message: str
    author: tuple[str, str]  # name, e-mail
    committer: tuple[str, str]  # name, e-mail
    is_merge: bool


def log(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[Commit]:
    """Every commit of the range, merges included, that is not at or below the baseline.
    Raises RuntimeError when git cannot answer or its output cannot be parsed; an unknown baseline is an error,
    never a silent pass."""
    args = ["log", "-z", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%P%x00%B", range_spec]
    if baseline:
        if _git(["cat-file", "-e", f"{baseline}^{{commit}}"], cwd).returncode != 0:
            raise RuntimeError(f"baseline commit {baseline[:12]} is not in this clone (fetch the full history)")
        args += ["--not", baseline]
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git log {range_spec} failed: {proc.stderr.strip()}")
    tokens = proc.stdout.split(_NUL)
    if tokens and tokens[-1] == "":  # `-z` ends every commit with a NUL
        tokens.pop()
    if len(tokens) % _FIELDS:
        raise RuntimeError(f"git log {range_spec}: unparseable output ({len(tokens)} fields, expected a multiple of "
                           f"{_FIELDS})")
    found: list[Commit] = []
    for i in range(0, len(tokens), _FIELDS):
        sha, author, author_email, committer, committer_email, parents, message = tokens[i:i + _FIELDS]
        sha = sha.strip()
        if not _SHA.match(sha):
            raise RuntimeError(f"git log {range_spec}: unparseable output (commit id {sha[:20]!r})")
        message = message.replace("\r\n", "\n")
        found.append(Commit(sha, message.split("\n", 1)[0], message, (author, author_email),
                            (committer, committer_email), len(parents.split()) > 1))
    return found


def commits(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[tuple[str, str]]:
    """(sha, first line) of the non-merge commits in the range that are not at or below the baseline."""
    return [(c.sha, c.subject) for c in log(range_spec, cwd, baseline) if not c.is_merge]


def attribution_reasons(commit: Commit) -> list[str]:
    """Why the message, author or committer of the commit carries an AI attribution (empty when it does not).
    The judgement is the repository's guard (aeos.core.attribution); its reasons are passed on as they are."""
    reasons = []
    findings = find_ai_attribution(commit.message)
    if findings:
        shown = "; ".join(f.describe() for f in findings[:MAX_FINDINGS_SHOWN])
        more = f" (+{len(findings) - MAX_FINDINGS_SHOWN} more)" if len(findings) > MAX_FINDINGS_SHOWN else ""
        reasons.append(f"AI attribution in the message - {shown}{more}")
    for role, (name, email) in (("author", commit.author), ("committer", commit.committer)):
        problem = ai_identity_problem(name, email)
        if problem:
            reasons.append(f"AI identity as {role}: {problem}")
    return reasons


def violations(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[tuple[str, str, str]]:
    """(sha, subject, reason) for every non-compliant commit of the range; the reasons of one commit are joined."""
    out = []
    for commit in log(range_spec, cwd, baseline):
        reasons = attribution_reasons(commit)
        if not commit.is_merge:
            subject_reason = check_subject(commit.subject)
            if subject_reason:
                reasons.insert(0, subject_reason)
        if reasons:
            out.append((commit.sha, commit.subject, "; ".join(reasons)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check commit subjects (Conventional Commits) and the no-attribution rule.")
    parser.add_argument("range", help="<base>..<head>")
    parser.add_argument("--baseline", default=BASELINE,
                        help="commits at or below this sha are not checked ('none' checks everything)")
    args = parser.parse_args(argv)
    baseline = None if args.baseline.lower() == "none" else args.baseline
    try:
        bad = violations(args.range, baseline=baseline)
        total = len(log(args.range, baseline=baseline))
    except (RuntimeError, OSError, ValueError) as exc:  # git missing or unparseable output: exit 2, never a traceback
        print(ascii_text(f"commit-subject: error: {exc}"))
        return 2
    for sha, subject, reason in bad:
        print(ascii_text(f"commit-subject: FAIL {sha[:12]} {subject!r}: {reason}"))
    if bad:
        print(f"commit-subject: {len(bad)} of {total} checked commit(s) do not comply")
        return 1
    print(f"commit-subject: {total} commit(s) checked, all comply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
