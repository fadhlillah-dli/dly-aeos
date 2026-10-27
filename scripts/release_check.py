"""Release tag check: does the commit tagged vX.Y.Z have everything a release needs?

  python scripts/release_check.py --tag v0.1.0 [--main origin/main] [--root <git checkout>]

Run it after the release pull request is merged and the merge commit is tagged, before the GitHub Release is
published. Every file is read from the TAGGED COMMIT (git show), never from the working tree, so a stale checkout
cannot make a wrong tag look right. Checks (one PASS/FAIL line each, then the verdict):

  1. the tag names a commit;
  2. the tag equals the version in EVERY version source (VERSION_SOURCES below: the one list that
     tests/unit/test_repository_hygiene.py uses too; backend/aeos/operations/release.py reads __version__ from
     backend/aeos/__init__.py on its own, and is not repeated here);
  3. CHANGELOG.md has a "## [X.Y.Z]" heading;
  4. changelog.d/ holds only its README.md (no fragment was left behind by scripts/changelog.py --assemble);
  5. docs/releases/vX.Y.Z.md exists;
  6. the tagged commit is an ancestor of --main (git merge-base --is-ancestor), so the tag sits on main.

The last line is always ``RELEASE_CHECK: PASS`` or ``RELEASE_CHECK: FAIL``. Exit 0 PASS, 1 FAIL, 2 usage error.
Stdlib only, ASCII output. Needs git. Run ``git fetch origin`` first so that origin/main is current.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_INIT_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_CFF_RE = re.compile(r"""^version:\s*["']?([^"'\s#]+)""", re.M)


def _pyproject(text: str) -> str | None:
    value = tomllib.loads(text).get("project", {}).get("version")
    return value if isinstance(value, str) else None


def _package_json(text: str) -> str | None:
    value = json.loads(text).get("version")
    return value if isinstance(value, str) else None


def _package_lock_root(text: str) -> str | None:
    value = json.loads(text).get("packages", {}).get("", {}).get("version")
    return value if isinstance(value, str) else None


def _regex(pattern: re.Pattern[str]) -> Callable[[str], str | None]:
    def read(text: str) -> str | None:
        found = pattern.search(text)
        return found.group(1) if found else None
    return read


# ONE list of the files that carry the release version. Add a new source here and both this script and the hygiene
# test pick it up.
VERSION_SOURCES: tuple[tuple[str, Callable[[str], str | None]], ...] = (
    ("pyproject.toml", _pyproject),
    ("frontend/package.json", _package_json),
    ("frontend/package-lock.json", _package_lock_root),
    ("CITATION.cff", _regex(_CFF_RE)),
    ("backend/aeos/__init__.py", _regex(_INIT_RE)),
)


def extract_version(rel: str, text: str) -> str | None:
    """The version that file *rel* declares, or None when it declares none or cannot be parsed."""
    readers = dict(VERSION_SOURCES)
    try:
        return readers[rel](text)
    except (ValueError, AttributeError, TypeError):  # JSON, TOML and shape errors
        return None


class GitError(Exception):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc


def _show(root: Path, commit: str, rel: str) -> str | None:
    proc = _git(root, "show", f"{commit}:{rel}")
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _names(root: Path, commit: str, rel_dir: str) -> list[str] | None:
    proc = _git(root, "ls-tree", "--name-only", f"{commit}:{rel_dir}")
    if proc.returncode != 0:
        return None
    return [n for n in proc.stdout.decode("utf-8", "replace").splitlines() if n]


Result = tuple[bool, str]


def check_tag(root: Path, tag: str, main_ref: str = "origin/main") -> list[Result]:
    """All checks for *tag* in the git checkout *root*; the caller prints them and decides the verdict."""
    found = TAG_RE.match(tag)
    if not found:
        return [(False, f"tag {tag!r} is not vMAJOR.MINOR.PATCH")]
    version = found.group(1)
    resolved = _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if resolved.returncode != 0:
        return [(False, f"tag {tag} does not exist in {root.name} (create it on the merge commit first)")]
    commit = resolved.stdout.decode("ascii", "replace").strip()
    results: list[Result] = [(True, f"tag {tag} -> commit {commit[:12]}")]

    for rel, _ in VERSION_SOURCES:
        text = _show(root, commit, rel)
        declared = None if text is None else extract_version(rel, text)
        if text is None:
            results.append((False, f"{rel}: missing at the tagged commit"))
        elif declared is None:
            results.append((False, f"{rel}: declares no readable version"))
        elif declared != version:
            results.append((False, f"{rel}: version is {declared}, the tag says {version}"))
        else:
            results.append((True, f"{rel}: version {declared}"))

    changelog = _show(root, commit, "CHANGELOG.md")
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)", re.M)
    if changelog is not None and heading.search(changelog):
        results.append((True, f"CHANGELOG.md: has '## [{version}]'"))
    else:
        results.append((False, f"CHANGELOG.md: no '## [{version}]' heading at the tagged commit"))

    names = _names(root, commit, "changelog.d")
    if names is None:
        results.append((False, "changelog.d/: missing at the tagged commit"))
    elif set(names) != {"README.md"}:
        left = sorted(set(names) - {"README.md"})
        results.append((False, "changelog.d/: " + (f"fragments left behind: {', '.join(left)}" if left
                                                  else "README.md is missing")))
    else:
        results.append((True, "changelog.d/: only README.md"))

    notes = f"docs/releases/v{version}.md"
    if _show(root, commit, notes) is None:
        results.append((False, f"{notes}: missing at the tagged commit"))
    else:
        results.append((True, f"{notes}: exists"))

    ancestor = _git(root, "merge-base", "--is-ancestor", commit, main_ref)
    if ancestor.returncode == 0:
        results.append((True, f"commit {commit[:12]} is an ancestor of {main_ref}"))
    elif ancestor.returncode == 1:
        results.append((False, f"commit {commit[:12]} is NOT an ancestor of {main_ref} (tag is not on main)"))
    else:
        detail = ancestor.stderr.decode("utf-8", "replace").strip()[:200] or f"exit {ancestor.returncode}"
        results.append((False, f"cannot compare with {main_ref}: {detail}"))
    return results


def run(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="release_check.py", description="Check a release tag before publishing.")
    parser.add_argument("--tag", required=True, metavar="vX.Y.Z", help="the release tag to check")
    parser.add_argument("--main", default="origin/main", help="ref the tagged commit must be on (default origin/main)")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="git checkout to read")
    args = parser.parse_args(argv)
    try:
        results = check_tag(Path(args.root), args.tag, args.main)
    except GitError as exc:
        results = [(False, str(exc))]
    for ok, message in results:
        out.write(f"{'PASS' if ok else 'FAIL'}: {message}".encode("ascii", "backslashreplace").decode("ascii") + "\n")
    passed = all(ok for ok, _ in results)
    out.write(f"RELEASE_CHECK: {'PASS' if passed else 'FAIL'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
