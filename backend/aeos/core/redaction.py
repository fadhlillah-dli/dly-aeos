"""Secret redaction for logs, traces, errors and evidence (§08, §25, §26, §50).

Two layers: pattern-based redaction of well-known credential shapes, plus an in-process registry
of secret values fetched at runtime so that exact values are scrubbed even if their shape is
unknown. Secret values are never persisted by this module.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"

# The end marker of a PEM private key block: part of the private-key pattern below, and what bounds the outgoing-content
# scan of that pattern to linear time (find_high_confidence_secrets).
_PEM_END = r"-----END [A-Z ]*PRIVATE KEY-----"

# (detector kind, pattern). The kind names what matched in a refusal or an audit record - never the matched text.
# Patterns with a capturing group (key=value forms) redact only their value part and are not high-confidence.
_NAMED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openrouter_key", re.compile(r"(?<![A-Za-z0-9])sk-or-v1-[A-Za-z0-9]{20,}")),  # OpenRouter
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{20,}")),  # Anthropic
    ("openai_style_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{20,}")),  # OpenAI-style
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),  # GitHub tokens
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),  # AWS access key id
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),  # Google API key
    ("google_oauth_token", re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),  # Google OAuth access token
    ("google_refresh_token", re.compile(r"1//[0-9A-Za-z_\-]{20,}")),  # Google refresh token
    # Vault/OpenBao service, batch, recovery tokens
    ("vault_token", re.compile(r"(?<![A-Za-z0-9])hv[sbr]\.[A-Za-z0-9_\-]{24,}")),
    # Vault/OpenBao tokens (older and short forms). Not glued to a preceding letter or digit, so code such as
    # ``this.handleSubmitButtonClickedEvent`` or ``lib.someLongFunctionNameHere`` is not a token (2026-10-28).
    ("vault_token_short", re.compile(r"(?<![A-Za-z0-9])(?:hvs|s|b)\.[A-Za-z0-9]{24,}")),
    ("openbao_recovery_token", re.compile(r"(?<![A-Za-z0-9.])r\.[A-Za-z0-9]{24,}")),  # OpenBao recovery token (short)
    # Vault/OpenBao `operator init` output. The label is required, so a bare base64 string (a lockfile
    # hash, a digest) never matches, nor does prose that only names an unseal key. ``\s*(?:\d+\s*)?:`` is the same
    # language as ``\s*\d*\s*:`` without its quadratic backtracking over a long run of whitespace (2026-10-28).
    ("vault_unseal_key", re.compile(
        r"(?i)\b(?:unseal|recovery) key(?: share)?\s*(?:\d+\s*)?:\s*[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=_\-])")),
    ("vault_root_token", re.compile(r"(?i)\binitial root token\s*:\s*[A-Za-z0-9._\-]{20,}(?![A-Za-z0-9._\-])")),
    ("vault_init_json", re.compile(
        r'(?i)"(?:unseal|recovery)_keys_(?:b64|hex)"\s*:\s*\[\s*"[A-Za-z0-9+/=]{32,}"'
        r'(?:\s*,\s*"[A-Za-z0-9+/=]{32,}")*'
    )),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?" + _PEM_END)),
    ("bearer_value", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]{16,}=*")),
    ("credential_assignment", re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']{6,}")),
]
_PATTERNS: list[re.Pattern[str]] = [p for _, p in _NAMED_PATTERNS]

# Key-name rule: a mapping value is masked when its key names a credential as a whole word
# (csrf_token, api_key, client_secret, Authorization) — but not counters/flags/hashes such as
# input_tokens, max_output_tokens, token_hash or office_authorization=True.
# Token shapes without the word-boundary guard, for scanning opaque/binary content where a credential
# can sit directly after arbitrary bytes (verifier binary scan). Never used for prose redaction.
UNBOUNDED_TOKEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p.pattern.replace("(?<![A-Za-z0-9])", "")) for p in _PATTERNS if not p.groups
]

_SENSITIVE_WORDS = frozenset(
    {"password", "passwd", "passphrase", "secret", "token", "authorization", "cookie", "credential", "credentials", "pat"}
)
_SENSITIVE_JOINED = ("apikey", "privatekey", "clientsecret", "secretid", "accesskey", "refreshtoken", "accesstoken")
_NON_SECRET_WORDS = frozenset(
    {"tokens", "count", "counts", "used", "usage", "limit", "limits", "max", "min", "budget", "ttl", "expiry",
     "expires", "hash", "hashes", "ref", "refs", "status", "type", "kind", "name", "id", "present", "required"}
)
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")

_lock = threading.Lock()
_known_values: set[str] = set()


def is_sensitive_key(key: str) -> bool:
    words = [w for w in _SPLIT.split(_CAMEL.sub(r"\1_\2", key).lower()) if w]
    if not words:
        return False
    joined = "".join(words)
    if any(j in joined for j in _SENSITIVE_JOINED):
        return True
    if any(w in _NON_SECRET_WORDS for w in words):
        return False
    return any(w in _SENSITIVE_WORDS for w in words)


def register_secret(value: str | None) -> None:
    """Register a runtime secret value so any later occurrence is scrubbed."""
    if value and len(value) >= 6:
        with _lock:
            _known_values.add(value)


def redact_text(text: str, *, pii: bool = False) -> str:
    """Scrub credentials; with ``pii=True`` also minimise personal identifiers (aeos.core.pii)."""
    if not text:
        return text
    with _lock:
        values = sorted(_known_values, key=len, reverse=True)
    for v in values:
        text = text.replace(v, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pat.sub(REDACTED, text)
    if pii:
        from aeos.core.pii import redact_pii

        text = redact_pii(text)
    return text


def redact(value: Any, *, pii: bool = False) -> Any:
    """Recursively redact strings, and fully mask values under sensitive keys. ``pii=True`` is used for
    stored traces/evidence/audit/events and exports (§50 "Trace berisi secret/PII")."""
    if isinstance(value, str):
        return redact_text(value, pii=pii)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and v and is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v, pii=pii)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, pii=pii) for v in value]
    return value


def contains_secret(text: str) -> bool:
    return redact_text(text) != text


REGISTERED_SECRET = "registered_secret"  # detector kind of a runtime secret value registered with register_secret()

_PEM_END_RE = re.compile(_PEM_END)
# A literal every match of the kind contains, checked on the lower-cased text before the pattern runs: a pattern that
# starts with a look-behind gets no literal-prefix search from the regex engine, so on a multi-megabyte text it costs
# tens of milliseconds even when nothing is there (the broker scans whole model requests, ADR-0127).
_PREFILTER: dict[str, tuple[str, ...]] = {
    "openrouter_key": ("sk-or-v1-",), "anthropic_key": ("sk-ant-",), "openai_style_key": ("sk-",),
    "vault_token": ("hvs.", "hvb.", "hvr."), "openbao_recovery_token": ("r.",),
    "vault_unseal_key": ("unseal key", "recovery key"), "vault_root_token": ("initial root token",),
    "vault_init_json": ("_keys_",), "private_key_pem": ("private key-----",),
}


@dataclass(frozen=True)
class SecretFinding:
    """One high-confidence match: its detector kind and its span in the scanned text (never the text itself)."""

    kind: str
    start: int
    end: int


def _iter_high_confidence(text: str) -> Iterator[SecretFinding]:
    with _lock:
        values = list(_known_values)
    for v in values:
        i = text.find(v)
        while i != -1:
            yield SecretFinding(REGISTERED_SECRET, i, i + len(v))
            i = text.find(v, i + len(v))
    lowered: str | None = None
    for kind, pat in _NAMED_PATTERNS:
        if pat.groups:
            continue
        hints = _PREFILTER.get(kind)
        if hints:
            lowered = text.lower() if lowered is None else lowered
            if not any(h in lowered for h in hints):
                continue
        endpos = len(text)
        if kind == "private_key_pem":
            # A BEGIN header with no END after it would make the lazy body scan run to the end of the text once per
            # header (quadratic). Every match ends with an END marker, so the scan stops at the last one: linear.
            endpos = max((m.end() for m in _PEM_END_RE.finditer(text)), default=0)
            if not endpos:
                continue
        for m in pat.finditer(text, 0, endpos):
            yield SecretFinding(kind, m.start(), m.end())


def find_high_confidence_secrets(text: str) -> list[SecretFinding]:
    """Every high-confidence match in *text* (the rule of contains_high_confidence_secret) with its detector kind and
    span, for a caller that refuses content and must name what it found without repeating it (the broker's outgoing
    model-request guard, ADR-0127). Time is linear in the length of the text."""
    return list(_iter_high_confidence(text)) if text else []


def contains_high_confidence_secret(text: str) -> bool:
    """Registered runtime secret values or token-shaped patterns only (no generic key=value rule),
    so prose like "token: string" is not a false positive."""
    if not text:
        return False
    return next(_iter_high_confidence(text), None) is not None
