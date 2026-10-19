"""Change-scope rules for candidate patches (§09, §21, §50 "Worker mengubah grader/policy/pipeline").

A worker may only change paths its TaskSpec allows. Independently of the TaskSpec, protected paths
(grader, policy, pipeline, broker, verifier, identity, migrations, install manifest) are never
accepted silently: a patch touching them is FAIL (not allowed by the TaskSpec) or QUARANTINED
(explicitly allowed — still candidate-only until special review and human promotion).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

# AGENTS.md "Protected paths (self-build, §09)" (the platform's own list) and the grader / pipeline /
# policy / agent-config / restricted-area paths protected for every managed project (§16, §21, §25):
# one shared definition with the compiler and the worker runtime (aeos.core.protected_paths).
from aeos.core.protected_paths import GENERIC_PROTECTED_PATHS, PLATFORM_PROTECTED_PATHS  # noqa: F401
from aeos.core.redaction import _PATTERNS as _REDACTION_PATTERNS
from aeos.core.redaction import UNBOUNDED_TOKEN_PATTERNS

# Files that could alter how the verifier's test process behaves rather than what it tests.
HARNESS_TAMPER_PATHS: tuple[str, ...] = (
    "**/sitecustomize.py",
    "**/usercustomize.py",
    "**/*.pth",
    "backend/conftest.py",
    "backend/**/conftest.py",
)
DOCS_ONLY_PATTERNS: tuple[str, ...] = ("docs/**", "**/*.md", "**/*.mdx", "**/*.rst", "README*", "LICENSE*", "CHANGELOG*")
# §31: routing/hooks/IAM/secrets/CI/migration/schema/flags/shared state/tools/instructions are high-impact
# even when the diff is small — test scope follows impact, not file count.
HIGH_IMPACT_PATTERNS: tuple[str, ...] = (
    "backend/aeos/routing/**", "backend/aeos/claude_bridge/**", "backend/aeos/identity/**", "backend/aeos/api/**",
    "backend/aeos/secrets/**", "backend/aeos/broker/**", "backend/aeos/policy/**", "backend/aeos/core/**",
    "backend/aeos/models/**", "backend/aeos/migrations/**", "backend/aeos/workforce/**", "**/facade.py",
    ".github/**", "policy/**", "install-manifest/**", "contracts/**", "scripts/**", "pyproject.toml",
    "frontend/package.json", "frontend/package-lock.json", "AGENTS.md", "CLAUDE.md", "**/.claude/**",
    "**/hooks/**", "config/**",
)
# §15 Security Reviewer: controls whose change needs a separate security review session.
SECURITY_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "backend/aeos/identity/**", "backend/aeos/secrets/**", "backend/aeos/policy/**", "backend/aeos/broker/**",
    "backend/aeos/api/deps.py", "backend/aeos/api/security.py", "backend/aeos/core/redaction.py",
    "backend/aeos/core/paths.py", "backend/aeos/worker_runtime/**", "backend/aeos/claude_bridge/**",
    ".github/workflows/**", "policy/**",
)

# Same allow-list as scripts/secret_scan.py: files that intentionally contain credential-shaped
# examples to test the redactor itself.
SECRET_SCAN_ALLOW = frozenset({
    "tests/unit/test_core.py",
    "tests/unit/test_identity.py",
    "backend/aeos/core/redaction.py",
    "scripts/secret_scan.py",
})
_HIGH_CONFIDENCE = [p for p in _REDACTION_PATTERNS if not p.groups]
# Binary content has no word boundaries: the binary scan uses aeos.core.redaction.UNBOUNDED_TOKEN_PATTERNS
# (no "not after an alphanumeric" guard) so a token glued to arbitrary bytes is still found (ADR-0060).

# Added lines in product code that reach into the test runner itself.
_TAMPER_CODE = re.compile(
    r"\b_pytest\b|\bpytest_(?:runtest|collect|configure|sessionfinish|sessionstart|report|terminal|unconfigure)\w*"
    r"|^\s*(?:import|from)\s+pytest\b|\bos\._exit\s*\("
)

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@lru_cache(maxsize=512)
def _glob_regex(pattern: str, case_insensitive: bool) -> re.Pattern[str]:
    i, out = 0, []
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
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE if case_insensitive else 0)


def normalize_pattern(pattern: str) -> str:
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.endswith("/"):
        p += "**"
    return p


def path_matches(path: str, pattern: str, *, case_insensitive: bool = False) -> bool:
    p = normalize_pattern(pattern)
    if not p:
        return False
    if not any(ch in p for ch in "*?"):
        a, b = (path.casefold(), p.casefold()) if case_insensitive else (path, p)
        return a == b or a.startswith(b.rstrip("/") + "/")
    return bool(_glob_regex(p, case_insensitive).match(path))


def first_match(path: str, patterns: list[str] | tuple[str, ...], *, case_insensitive: bool = False) -> str | None:
    for pat in patterns:
        if path_matches(path, pat, case_insensitive=case_insensitive):
            return pat
    return None


def unsafe_path_reason(path: str) -> str | None:
    """Reject paths that could escape or be ambiguous on Windows/macOS/Linux checkouts."""
    if not path or "\x00" in path:
        return "empty or NUL path"
    if "\\" in path or ":" in path:
        return "backslash or colon in path"
    pp = PurePosixPath(path)
    if pp.is_absolute():
        return "absolute path"
    for seg in pp.parts:
        if seg in ("..", "."):
            return "traversal segment"
        if seg.split(".")[0].casefold() in _WINDOWS_RESERVED:
            return f"Windows reserved name '{seg}'"
        if seg.endswith((" ", ".")):
            return "trailing dot/space segment"
    return None


@dataclass
class ScopeViolation:
    path: str
    rule: str  # PROTECTED_PATH | PROTECTED_PATH_ALLOWED | FORBIDDEN_PATH | OUTSIDE_ALLOWED | HARNESS_TAMPER | UNSAFE_PATH
    pattern: str | None = None
    detail: str = ""


@dataclass
class ScopeResult:
    changed: list[str]
    allowed: list[str]
    forbidden: list[str]
    protected: list[str]
    violations: list[ScopeViolation] = field(default_factory=list)

    @property
    def failing(self) -> list[ScopeViolation]:
        return [v for v in self.violations if v.rule != "PROTECTED_PATH_ALLOWED"]

    @property
    def protected_touched(self) -> list[str]:
        return sorted({v.path for v in self.violations if v.rule in ("PROTECTED_PATH", "PROTECTED_PATH_ALLOWED")})

    @property
    def status(self) -> str:
        if self.failing:
            return "FAIL"
        if self.protected_touched:
            return "QUARANTINED"
        return "PASS"

    @property
    def quarantine_reason(self) -> str | None:
        touched = self.protected_touched
        if not touched:
            return None
        return (
            "Protected path(s) changed: " + ", ".join(touched[:20])
            + " — the patch stays candidate-only; special review and human promotion are required (§09, §21)."
        )

    @property
    def docs_only(self) -> bool:
        return bool(self.changed) and all(first_match(p, DOCS_ONLY_PATTERNS) for p in self.changed)

    @property
    def high_impact(self) -> list[str]:
        return [p for p in self.changed if first_match(p, HIGH_IMPACT_PATTERNS, case_insensitive=True)]

    @property
    def security_sensitive(self) -> list[str]:
        return [p for p in self.changed if first_match(p, SECURITY_SENSITIVE_PATTERNS, case_insensitive=True)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "impact": "HIGH" if self.high_impact else "NORMAL",
            "high_impact_paths": self.high_impact,
            "security_sensitive_paths": self.security_sensitive,
            "status": self.status,
            "changed_files": self.changed,
            "allowed_paths": self.allowed,
            "forbidden_paths": self.forbidden,
            "protected_paths": self.protected,
            "violations": [asdict(v) for v in self.violations],
            "protected_touched": self.protected_touched,
            "quarantine_reason": self.quarantine_reason,
            "docs_only": self.docs_only,
        }


def check_scope(changed: list[str], *, allowed: list[str], forbidden: list[str], protected: list[str]) -> ScopeResult:
    result = ScopeResult(changed=sorted(set(changed)), allowed=list(allowed), forbidden=list(forbidden),
                         protected=list(protected))
    for path in result.changed:
        reason = unsafe_path_reason(path)
        if reason:
            result.violations.append(ScopeViolation(path, "UNSAFE_PATH", None, reason))
            continue
        tamper = first_match(path, HARNESS_TAMPER_PATHS, case_insensitive=True)
        if tamper:
            result.violations.append(ScopeViolation(path, "HARNESS_TAMPER", tamper,
                                                    "file can alter the verifier's interpreter/test runner"))
        allowed_pat = first_match(path, allowed)
        prot = first_match(path, protected, case_insensitive=True)
        if prot:
            rule = "PROTECTED_PATH_ALLOWED" if allowed_pat else "PROTECTED_PATH"
            detail = ("TaskSpec allows this protected path; quarantined for special review" if allowed_pat
                      else "protected path changed outside the TaskSpec contract")
            result.violations.append(ScopeViolation(path, rule, prot, detail))
        forb = first_match(path, forbidden, case_insensitive=True)
        if forb:
            result.violations.append(ScopeViolation(path, "FORBIDDEN_PATH", forb, "TaskSpec forbids this path"))
        if not allowed_pat and not prot:
            detail = "TaskSpec declares no allowed_paths" if not allowed else "outside TaskSpec allowed_paths"
            result.violations.append(ScopeViolation(path, "OUTSIDE_ALLOWED", None, detail))
    return result


@dataclass
class AddedLine:
    path: str
    line: int
    text: str


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_UNKNOWN_PATH = "<unparsed-path>"


def added_lines(diff_text: str) -> list[AddedLine]:
    """Added lines of a unified diff with their new-file line numbers.

    Hunk bodies are consumed by their declared line counts, so an *added line* that itself starts
    with ``++ `` / ``-- `` / ``diff --git`` can never be mistaken for a file header (which would let a
    patch re-attribute its following lines to an allow-listed path and dodge the secret scan).
    """
    out: list[AddedLine] = []
    path: str | None = None
    new_line = 0
    old_left = new_left = 0
    for raw in diff_text.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if old_left > 0 or new_left > 0:
            if raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            tag, body = raw[:1], raw[1:]
            if tag == "+":
                out.append(AddedLine(path or _UNKNOWN_PATH, new_line, body))
                new_line += 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            else:  # context line (" " or an empty line from a stripped trailing space)
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("diff --git "):
            path = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else (target or _UNKNOWN_PATH)
            continue
        m = _HUNK.match(raw)
        if m:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_line = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            if path is None:
                path = _UNKNOWN_PATH  # hunk without a parsable header: scan it, never skip it
    return out


def scan_added_secrets(lines: list[AddedLine]) -> list[dict[str, Any]]:
    """High-confidence credential shapes in added lines. Findings never include the value."""
    findings: list[dict[str, Any]] = []
    for al in lines:
        if al.path in SECRET_SCAN_ALLOW:
            continue
        for pat in _HIGH_CONFIDENCE:
            if pat.search(al.text):
                findings.append({"path": al.path, "line": al.line, "pattern": pat.pattern[:32] + "..."})
                break
    return findings


BINARY_SCAN_MAX_BYTES = 20 * 1024 * 1024


def scan_binary_secrets(path: str, data: bytes) -> list[dict[str, Any]]:
    """High-confidence credential shapes inside a changed *binary* file.

    ``git diff`` prints only "Binary files differ" for files containing NUL bytes, so the added-line
    scan never sees them; without this a credential could be committed inside such a file unseen.
    Findings never include the value; offsets are byte offsets (latin-1 view of the bytes). Only the
    first ``BINARY_SCAN_MAX_BYTES`` are scanned — the caller reports larger files as NOT-PROVEN.
    """
    if path in SECRET_SCAN_ALLOW:
        return []
    text = data[:BINARY_SCAN_MAX_BYTES].decode("latin-1")
    findings: list[dict[str, Any]] = []
    # Unbounded shapes: inside binaries a token can follow arbitrary bytes (e.g. b"PKghp_...").
    for pat in UNBOUNDED_TOKEN_PATTERNS:
        m = pat.search(text)
        if m:
            findings.append({"path": path, "line": None, "offset": m.start(), "pattern": pat.pattern[:32] + "...",
                             "binary": True})
    return findings


def scan_harness_tamper(lines: list[AddedLine]) -> list[dict[str, Any]]:
    """Product code (backend/, non-test) reaching into the test runner is treated as tampering."""
    findings: list[dict[str, Any]] = []
    for al in lines:
        if not al.path.startswith("backend/") or not al.path.endswith(".py"):
            continue
        if al.path.startswith("backend/aeos/verification/"):
            continue  # the verifier itself is a protected path (quarantined separately)
        name = al.path.rsplit("/", 1)[-1]
        if name.startswith("test_") or name == "conftest.py":
            continue
        if _TAMPER_CODE.search(al.text):
            findings.append({"path": al.path, "line": al.line, "rule": "HARNESS_TAMPER_CODE"})
    return findings
