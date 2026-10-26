"""Commit-subject gate: Conventional Commits `type(scope)?!?: description` (CONTRIBUTING.md, "Rules for every
change"). Stdlib only, ASCII-only output.

    python scripts/commit_subject.py <base>..<head>      exit 0 ok, 1 violations, 2 usage or git error

Merge commits (two or more parents) are exempt. Commits at or below BASELINE are never checked."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "test", "ci", "build", "chore", "refactor", "perf", "style", "revert", "security")
MAX_LENGTH = 100
PATTERN = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([^()\s]+\))?!?: \S.*$")

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
    """None when the subject complies, else the reason."""
    if len(subject) > MAX_LENGTH:
        return f"first line is {len(subject)} characters (maximum {MAX_LENGTH})"
    if not PATTERN.match(subject):
        return "expected 'type(scope)?!?: description' with type " + "|".join(TYPES)
    return None


def _git(args: list[str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, check=False)


def commits(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[tuple[str, str]]:
    """(sha, first line) of the non-merge commits in the range that are not at or below the baseline.
    Raises RuntimeError when git cannot answer; an unknown baseline is an error, never a silent pass."""
    args = ["log", "--no-merges", f"--format=%H{_FIELD}%B{_RECORD}", range_spec]
    if baseline:
        if _git(["cat-file", "-e", f"{baseline}^{{commit}}"], cwd).returncode != 0:
            raise RuntimeError(f"baseline commit {baseline[:12]} is not in this clone (fetch the full history)")
        args += ["--not", baseline]
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git log {range_spec} failed: {proc.stderr.strip()}")
    found: list[tuple[str, str]] = []
    for record in proc.stdout.split(_RECORD):
        if _FIELD in record:
            sha, body = record.strip("\n").split(_FIELD, 1)
            found.append((sha.strip(), body.split("\n", 1)[0].rstrip("\r")))
    return found


def violations(range_spec: str, cwd: str | None = None, baseline: str | None = BASELINE) -> list[tuple[str, str, str]]:
    """(sha, subject, reason) for every non-compliant commit of the range."""
    out = []
    for sha, subject in commits(range_spec, cwd, baseline):
        reason = check_subject(subject)
        if reason:
            out.append((sha, subject, reason))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check commit subjects against Conventional Commits.")
    parser.add_argument("range", help="<base>..<head>")
    parser.add_argument("--baseline", default=BASELINE,
                        help="commits at or below this sha are not checked ('none' checks everything)")
    args = parser.parse_args(argv)
    baseline = None if args.baseline.lower() == "none" else args.baseline
    try:
        bad = violations(args.range, baseline=baseline)
        total = len(commits(args.range, baseline=baseline))
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
