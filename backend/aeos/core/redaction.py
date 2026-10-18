"""Secret redaction for logs, traces, errors and evidence (§08, §25, §26, §50).

Two layers: pattern-based redaction of well-known credential shapes, plus an in-process registry
of secret values fetched at runtime so that exact values are scrubbed even if their shape is
unknown. Secret values are never persisted by this module.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?<![A-Za-z0-9])sk-or-v1-[A-Za-z0-9]{20,}"),  # OpenRouter
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI-style
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"(?<![A-Za-z0-9])ya29\.[0-9A-Za-z_\-]{20,}"),  # Google OAuth access token
    re.compile(r"(?<![A-Za-z0-9])1//[0-9A-Za-z_\-]{20,}"),  # Google refresh token
    re.compile(r"(?:hvs|s|b)\.[A-Za-z0-9]{24,}"),  # Vault/OpenBao tokens
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]{16,}=*"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']{6,}"),
]

# Key-name rule: a mapping value is masked when its key names a credential as a whole word
# (csrf_token, api_key, client_secret, Authorization) — but not counters/flags/hashes such as
# input_tokens, max_output_tokens, token_hash or office_authorization=True.
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


def redact_text(text: str) -> str:
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
    return text


def redact(value: Any) -> Any:
    """Recursively redact strings, and fully mask values under sensitive keys."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and v and is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def contains_secret(text: str) -> bool:
    return redact_text(text) != text


def contains_high_confidence_secret(text: str) -> bool:
    """Registered runtime secret values or token-shaped patterns only (no generic key=value rule),
    so prose like "token: string" is not a false positive."""
    if not text:
        return False
    with _lock:
        values = list(_known_values)
    if any(v in text for v in values):
        return True
    return any(p.search(text) for p in _PATTERNS if not p.groups)
