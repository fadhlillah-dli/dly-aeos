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
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),  # OpenRouter
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),  # Google OAuth access token
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),  # Google refresh token
    re.compile(r"(?:hvs|s|b)\.[A-Za-z0-9]{24,}"),  # Vault/OpenBao tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]{16,}=*"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']{6,}"),
]

_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)")

_lock = threading.Lock()
_known_values: set[str] = set()


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
            if isinstance(k, str) and _SENSITIVE_KEYS.search(k) and v not in (None, "", False):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def contains_secret(text: str) -> bool:
    return redact_text(text) != text
