"""Path scope rules for TaskSpecs (§19 write sets, §21 allowed/forbidden paths, §09 protected paths).

Patterns are repo-relative POSIX globs: ``dir/``, ``dir/**``, ``src/*.ts``, ``exact/file.py``.
Matching against forbidden and protected paths is case-insensitive (Windows/macOS file systems are
case-insensitive, so ``Policy/x`` must not slip past ``policy/**``); allowed paths match
case-sensitively so that a case mismatch is treated as a violation, never as permission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# AGENTS.md "Protected paths (self-build, §09)" — patches touching these are quarantined.
PLATFORM_PROTECTED_PATHS: tuple[str, ...] = (
    "policy/**",
    "backend/aeos/policy/**",
    "backend/aeos/verification/**",
    "backend/aeos/broker/**",
    "backend/aeos/identity/**",
    "tests/acceptance/**",
    ".github/workflows/**",
    "install-manifest/**",
    "backend/aeos/migrations/**",
)
# §21/§31: trusted pipeline definitions of any project.
GENERIC_PROTECTED_PATHS: tuple[str, ...] = (
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "bitbucket-pipelines.yml",
    "azure-pipelines.yml",
    ".circleci/**",
)
_GLOB_CHARS = re.compile(r"[*?\[]")


class ScopePathError(ValueError):
    """A path that is absolute, escapes the repository or is otherwise not a plain relative path."""


def normalize(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ScopePathError("empty path")
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("~"):
        raise ScopePathError(f"absolute path not allowed: {path!r}")
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ScopePathError(f"path escapes repository: {path!r}")
        parts.append(seg)
    if not parts:
        raise ScopePathError(f"empty path: {path!r}")
    return "/".join(parts)


def normalize_pattern(pattern: str) -> str:
    trailing = pattern.strip().replace("\\", "/").endswith("/")
    p = normalize(pattern)
    return p + "/**" if trailing else p


@lru_cache(maxsize=1024)
def _regex(pattern: str, ci: bool) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    # A plain directory/file pattern also covers everything below it.
    if not _GLOB_CHARS.search(pattern):
        body += "(?:/.*)?"
    return re.compile(f"^{body}$", re.IGNORECASE if ci else 0)


def matches(path: str, pattern: str, *, case_insensitive: bool = False) -> bool:
    try:
        return bool(_regex(normalize_pattern(pattern), case_insensitive).match(normalize(path)))
    except ScopePathError:
        return False


def matches_any(path: str, patterns: list[str] | tuple[str, ...], *, case_insensitive: bool = False) -> str | None:
    for pat in patterns:
        if matches(path, pat, case_insensitive=case_insensitive):
            return pat
    return None


def static_prefix(pattern: str) -> str:
    """Leading literal directory segments of a pattern (``a/b/*.py`` -> ``a/b``)."""
    p = normalize_pattern(pattern)
    m = _GLOB_CHARS.search(p)
    if m is None:
        return p
    head = p[: m.start()]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test: two write patterns overlap when one literal prefix contains the
    other (case-insensitive). False negatives would let two workers write the same file, so any
    doubt resolves to "overlap" (§19/§22 shared files are serialised)."""
    pa, pb = static_prefix(a).lower(), static_prefix(b).lower()
    if not pa or not pb:
        return True
    if pa == pb:
        return True
    return pa.startswith(pb + "/") or pb.startswith(pa + "/")


def write_sets_overlap(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    """Overlapping pattern pairs. An empty allowed_paths list means "whole repository"."""
    a = a or ["**"]
    b = b or ["**"]
    return [(x, y) for x in a for y in b if patterns_overlap(x, y)]


@dataclass
class ChangedPath:
    path: str
    status: str  # A|M|D|R|C|T (git name-status)
    mode: str | None = None  # "120000" for symlinks
    old_path: str | None = None


@dataclass
class ScopeResult:
    violations: list[dict[str, str]] = field(default_factory=list)  # outside allowed / forbidden / unsafe
    protected_hits: list[dict[str, str]] = field(default_factory=list)  # quarantine for special review

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def quarantine(self) -> bool:
        return bool(self.violations or self.protected_hits)


def check_scope(
    changes: list[ChangedPath], *, allowed: list[str], forbidden: list[str], protected: tuple[str, ...] | list[str],
) -> ScopeResult:
    result = ScopeResult()
    for ch in changes:
        for p in filter(None, (ch.path, ch.old_path)):
            try:
                norm = normalize(p)
            except ScopePathError as exc:
                result.violations.append({"path": p, "rule": "unsafe-path", "detail": str(exc)})
                continue
            if norm.split("/", 1)[0].lower() == ".git":
                result.violations.append({"path": norm, "rule": "git-internals", "detail": "write to .git"})
                continue
            hit = matches_any(norm, forbidden, case_insensitive=True)
            if hit:
                result.violations.append({"path": norm, "rule": "forbidden-path", "detail": hit})
                continue
            if allowed and not matches_any(norm, allowed):
                result.violations.append({"path": norm, "rule": "outside-allowed-paths", "detail": ",".join(allowed)})
                continue
            if ch.mode == "120000":
                result.protected_hits.append({"path": norm, "rule": "symlink", "detail": "symlink added/changed"})
            prot = matches_any(norm, list(protected), case_insensitive=True)
            if prot:
                result.protected_hits.append({"path": norm, "rule": "protected-path", "detail": prot})
    return result
