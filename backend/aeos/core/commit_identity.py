"""Code-host commit identities (owner rule 2026-10-26; AGENTS.md invariant 11).

Every activity on GitHub uses the owner's GitHub identity; every activity on Bitbucket uses the owner's company
identity (two different addresses). Both live only in the gitignored ``aeos.yaml``::

    commit_identities:
      github:    {name: Your Name, email: you@example.com}
      bitbucket: {name: Your Name, email: you@company.example.com}

``commit_author_name`` / ``commit_author_email`` stay a backward-compatible fallback for **github only**.
Nothing else ever falls back: without a valid identity for the code host, worker candidates are committed
locally with the runtime identity and marked not pushable, and the broker refuses the push (fail closed).

The broker checks every commit of the pushed range against the identity of its provider
(``commit_problems``): author e-mail == committer e-mail == the configured address, no AI/bot identity, no AI
attribution in the message (``aeos.core.attribution``). Records keep the address minimised (``redact_pii``).
Pure functions: settings in, results out - git is run by the callers (broker mirror, candidate workspace).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aeos.core.attribution import ai_identity_problem, find_ai_attribution

GITHUB = "github"
BITBUCKET = "bitbucket"
PROVIDERS: tuple[str, ...] = (GITHUB, BITBUCKET)
CONFIG_KEY = "commit_identities"
LEGACY_KEYS = "commit_author_name/commit_author_email"
CONFIGURED, MISSING, INVALID = "CONFIGURED", "MISSING", "INVALID"
# A run's candidate range is a handful of commits; a push whose range cannot be bounded by the run's base
# commit checks the whole history up to this many commits and refuses beyond it (fail closed).
MAX_CHECKED_COMMITS = 1000

_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_NAME = re.compile(r"^[^<>\n\r\x00]{1,100}$")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IDENT = re.compile(r"^(?P<name>.*?) ?<(?P<email>[^<>]*)> (?P<ts>-?\d+)(?: (?P<tz>[+-]\d{4}))?$")

CODE_IDENTITY_MISMATCH = "COMMIT_IDENTITY_MISMATCH"
CODE_ATTRIBUTION = "AI_ATTRIBUTION_IN_COMMIT"
CODE_MISSING = "COMMIT_IDENTITY_MISSING"
CODE_RANGE = "COMMIT_RANGE_UNBOUNDED"
REFUSAL_CODES = frozenset({CODE_IDENTITY_MISMATCH, CODE_ATTRIBUTION, CODE_MISSING, CODE_RANGE})


def config_key(provider: str) -> str:
    return f"{CONFIG_KEY}.{provider}"


def provider_for(domain: str | None, repo_provider: str | None = None) -> str:
    """The code host of a project (U-02..U-04, the broker's routing): OFFICE -> Bitbucket; a registered
    Bitbucket repository -> Bitbucket; everything else -> GitHub."""
    if str(domain or "").upper() == "OFFICE" or str(repo_provider or "").upper() == "BITBUCKET":
        return BITBUCKET
    return GITHUB


def validate_identity(name: Any, email: Any) -> str | None:
    """Why ``name``/``email`` cannot be a commit identity (None = valid). Never echoes the values."""
    if not isinstance(name, str) or not name.strip():
        return "name is missing"
    if not isinstance(email, str) or not email.strip():
        return "email is missing"
    if not _NAME.match(name.strip()):
        return "name is not a plain name (1-100 characters, no <, >, newline)"
    if not _EMAIL.match(email.strip()):
        return "email is not an e-mail address"
    ai = ai_identity_problem(name.strip(), email.strip())
    if ai:
        return f"{ai}: no AI or bot may author or commit on a code host"
    return None


@dataclass(frozen=True)
class CommitIdentity:
    provider: str
    status: str  # CONFIGURED | MISSING | INVALID
    name: str | None = None
    email: str | None = None
    source: str = ""  # the aeos.yaml key the identity came from ("" when MISSING)
    problem: str = ""

    @property
    def configured(self) -> bool:
        return self.status == CONFIGURED and bool(self.email)

    @property
    def key(self) -> str:
        return config_key(self.provider)

    def __repr__(self) -> str:  # the address never lands in a log line by accident
        return f"CommitIdentity(provider={self.provider!r}, status={self.status!r}, source={self.source!r})"


def resolve(settings: Any, provider: str) -> CommitIdentity:
    """The configured identity of *provider* (``commit_identities.<provider>``; github falls back to the legacy
    ``commit_author_*`` keys). A present but invalid entry is INVALID - never silently replaced by the fallback."""
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        return CommitIdentity(provider or "unknown", INVALID, problem=f"unknown code-host provider {provider!r}")
    mapping = getattr(settings, CONFIG_KEY, None) or {}
    entry: Any = None
    present = False
    if isinstance(mapping, dict):
        for k, v in mapping.items():
            if str(k).strip().lower() == provider:
                entry, present = v, True
    if present:
        if not isinstance(entry, dict):
            return CommitIdentity(provider, INVALID, source=config_key(provider),
                                  problem="must be a mapping with name and email")
        name, email = entry.get("name"), entry.get("email")
        problem = validate_identity(name, email)
        if problem:
            return CommitIdentity(provider, INVALID, source=config_key(provider), problem=problem)
        return CommitIdentity(provider, CONFIGURED, str(name).strip(), str(email).strip(), config_key(provider))
    if provider == GITHUB:
        name, email = getattr(settings, "commit_author_name", None), getattr(settings, "commit_author_email", None)
        if name or email:
            problem = validate_identity(name, email)
            if problem:
                return CommitIdentity(provider, INVALID, source=LEGACY_KEYS, problem=problem)
            return CommitIdentity(provider, CONFIGURED, str(name).strip(), str(email).strip(), LEGACY_KEYS)
    return CommitIdentity(provider, MISSING, problem=f"{config_key(provider)} is not set in aeos.yaml")


def missing_message(ident: CommitIdentity) -> str:
    what = "is invalid" if ident.status == INVALID else "is not configured"
    why = f" ({ident.problem})" if ident.problem and ident.status == INVALID else ""
    return (f"the {ident.provider} commit identity {what}{why}: set {ident.key} (name, email) in aeos.yaml - every "
            f"commit pushed to {ident.provider} is authored and committed by the owner's {ident.provider} identity "
            "(owner rule, AGENTS.md invariant 11); nothing is pushed without it")


# ----------------------------------------------------------------------------- commits of a pushed range
@dataclass(frozen=True)
class CommitRecord:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str


def parse_commit_object(sha: str, raw: str) -> CommitRecord:
    """Parse a raw commit object (``git cat-file commit <sha>``): headers, a blank line, the message. Header
    continuation lines (gpgsig, mergetag) start with a space. An unparsable identity is kept empty, which never
    matches a configured address (fail closed)."""
    head, sep, message = raw.partition("\n\n")
    if not sep:
        head, message = raw, ""
    idents: dict[str, tuple[str, str]] = {}
    for line in head.split("\n"):
        if line.startswith(" "):
            continue
        key, _, value = line.partition(" ")
        if key in ("author", "committer") and key not in idents:
            m = _IDENT.match(value.rstrip("\r"))
            idents[key] = (m.group("name"), m.group("email")) if m else ("", "")
    author = idents.get("author", ("", ""))
    committer = idents.get("committer", ("", ""))
    return CommitRecord(sha=sha, author_name=author[0], author_email=author[1], committer_name=committer[0],
                        committer_email=committer[1], message=message)


def parse_cat_file_batch(data: bytes) -> list[CommitRecord]:
    """Parse ``git cat-file --batch`` output (``<sha> <type> <size>\\n<size bytes>\\n`` per object). Sizes are
    honoured exactly, so no message content can forge another record. A missing or non-commit object raises."""
    out: list[CommitRecord] = []
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise ValueError("truncated cat-file header")
        header = data[pos:nl].decode("ascii", "replace").split()
        if len(header) != 3 or header[1] != "commit" or not header[2].isdigit():
            raise ValueError(f"not a commit object: {' '.join(header)[:80]}")
        size = int(header[2])
        body = data[nl + 1:nl + 1 + size]
        if len(body) != size:
            raise ValueError("truncated cat-file object")
        out.append(parse_commit_object(header[0], body.decode("utf-8", "replace")))
        pos = nl + 1 + size + 1  # the object is followed by one LF
    return out


def valid_sha(value: str) -> bool:
    return bool(_SHA.match(value or ""))


def commit_problems(commits: Sequence[CommitRecord], *, provider: str, expected_email: str) -> list[dict[str, str]]:
    """Problems of the commits about to be pushed to *provider* (empty = pushable). Addresses never appear in
    the result: only the commit, the rule and which side (author/committer) broke it."""
    want = (expected_email or "").strip().lower()
    out: list[dict[str, str]] = []
    for c in commits:
        wrong = [side for side, email in (("author", c.author_email), ("committer", c.committer_email))
                 if not want or email.strip().lower() != want]
        if wrong:
            out.append({"sha": c.sha, "code": CODE_IDENTITY_MISMATCH,
                        "detail": f"{' and '.join(wrong)} e-mail is not the configured {provider} identity "
                                  f"({config_key(provider)})"})
        for side, name, email in (("author", c.author_name, c.author_email),
                                  ("committer", c.committer_name, c.committer_email)):
            why = ai_identity_problem(name, email)
            if why:
                out.append({"sha": c.sha, "code": CODE_ATTRIBUTION, "detail": f"{side}: {why}"})
        findings = find_ai_attribution(c.message)
        if findings:
            out.append({"sha": c.sha, "code": CODE_ATTRIBUTION,
                        "detail": "message: " + "; ".join(f.describe() for f in findings[:3])})
    return out


def refusal_code(problems: Sequence[dict[str, str]]) -> str:
    """AI attribution outranks an identity mismatch (both refuse the push)."""
    codes = {p.get("code") for p in problems}
    if CODE_ATTRIBUTION in codes:
        return CODE_ATTRIBUTION
    if CODE_RANGE in codes:
        return CODE_RANGE
    return CODE_IDENTITY_MISMATCH
