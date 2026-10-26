"""Commit-message gate (CONTRIBUTING.md, "Rules for every change"): the first line is a Conventional Commits subject
`type(scope)?!?: description`, and no commit carries an AI attribution (co-author trailer, "generated with" line,
tool author or committer e-mail). Stdlib only, ASCII-only output.

    python scripts/commit_subject.py <base>..<head>      exit 0 ok, 1 violations, 2 usage or git error

Merge commits (two or more parents) are exempt from the subject format but not from the attribution check (GitHub
writes their message). Commits at or below BASELINE are never checked. `Revert "<subject>"` (what `git revert` and
GitHub's revert button write) passes when the quoted subject passes."""

from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "test", "ci", "build", "chore", "refactor", "perf", "style", "revert", "security")
MAX_LENGTH = 100
PATTERN = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([^()\s]+\))?!?: \S.*$")
REVERT = re.compile(r'^Revert "(.+)"$')

# The repository is public: AI tools must never appear as author, co-author or in an attribution line (owner rule,
# CONTRIBUTING.md "No attribution lines"). (reason, where, pattern), all case-insensitive; "message" patterns are
# matched per line of the full message, "email" patterns against the author and the committer e-mail.
_AI_NAMES = r"claude|anthropic|openai|chatgpt|copilot|gemini|cursor"
_AI_HOSTS = r"@(?:[\w-]+\.)*(?:anthropic|openai)\.com"
ATTRIBUTION_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("co-author trailer naming an AI tool",
     "message", re.compile(rf"^\s*co-authored-by:.*(?:\b(?:{_AI_NAMES})\b|{_AI_HOSTS})", re.IGNORECASE)),
    ("'Generated with' line naming an AI tool",
     "message", re.compile(rf"\bgenerated with\b.*\b(?:{_AI_NAMES}|codex)\b", re.IGNORECASE)),
    ("link to Claude Code",
     "message", re.compile(r"claude\.ai/code|claude\.com/claude-code", re.IGNORECASE)),
    ("author or committer e-mail of an AI vendor",
     "email", re.compile(rf"{_AI_HOSTS}\s*>?$", re.IGNORECASE)),
)

# Nothing at or below this commit is checked, so the gate is green on the day it landed. It is origin/main as of
# 2026-10-26 (merge of PR #16). Of the 186 non-merge commits up to it, 30 have a subject over 100 characters and 1
# has a scope list ("ci,docs:") that Conventional Commits does not allow; rewriting a protected main is not an option.
# Only commits after it must comply. Moving this constant later only widens what is exempt: do it with the owner.
BASELINE = "bd54f897198fae6236d9143a5db00cddff923fd2"

_FIELD, _RECORD = "\x1f", "\x1e"


def ascii_text(text: str) -> str:
    """Printable ASCII only (legacy Windows consoles): anything else becomes '?'."""
    return text.encode("ascii", "replace").decode("ascii")


def check_subject(subject: str) -> str | None:
    """None when the subject complies, else the reason. `Revert "<subject>"` complies when the quoted subject does
    (the wrapper is exempt from the length limit, the quoted subject is not)."""
    revert = REVERT.match(subject)
    if revert:
        inner = check_subject(revert.group(1))
        return None if inner is None else f"reverted subject does not comply: {inner}"
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
    emails: tuple[str, str]  # author, committer
    is_merge: bool


def log(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[Commit]:
    """Every commit of the range, merges included, that is not at or below the baseline.
    Raises RuntimeError when git cannot answer; an unknown baseline is an error, never a silent pass."""
    args = ["log", f"--format=%H{_FIELD}%P{_FIELD}%ae{_FIELD}%ce{_FIELD}%B{_RECORD}", range_spec]
    if baseline:
        if _git(["cat-file", "-e", f"{baseline}^{{commit}}"], cwd).returncode != 0:
            raise RuntimeError(f"baseline commit {baseline[:12]} is not in this clone (fetch the full history)")
        args += ["--not", baseline]
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git log {range_spec} failed: {proc.stderr.strip()}")
    found: list[Commit] = []
    for record in proc.stdout.split(_RECORD):
        if _FIELD not in record:
            continue
        sha, parents, author, committer, message = record.strip("\n").split(_FIELD, 4)
        message = message.replace("\r\n", "\n")
        found.append(Commit(sha.strip(), message.split("\n", 1)[0], message, (author.strip(), committer.strip()),
                            len(parents.split()) > 1))
    return found


def commits(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[tuple[str, str]]:
    """(sha, first line) of the non-merge commits in the range that are not at or below the baseline."""
    return [(c.sha, c.subject) for c in log(range_spec, cwd, baseline) if not c.is_merge]


def attribution_reasons(commit: Commit) -> list[str]:
    """Why the message, author or committer of the commit carries an AI attribution (empty when it does not)."""
    reasons = []
    for reason, where, pattern in ATTRIBUTION_PATTERNS:
        if where == "email":
            hit = any(pattern.search(email) for email in commit.emails)
        else:
            hit = any(pattern.search(line) for line in commit.message.split("\n"))
        if hit:
            reasons.append(reason)
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
    except RuntimeError as exc:
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
