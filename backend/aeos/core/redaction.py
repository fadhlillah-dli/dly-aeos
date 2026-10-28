"""Secret redaction for logs, traces, errors and evidence (§08, §25, §26, §50).

Two layers: pattern-based redaction of well-known credential shapes, plus an in-process registry
of secret values fetched at runtime so that exact values are scrubbed even if their shape is
unknown. Secret values are never persisted by this module.
"""

from __future__ import annotations

import re
import threading
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import accumulate
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
    # Vault/OpenBao tokens (older and short forms). No look-behind on purpose: a token glued to a letter or digit (after
    # a JSON-escaped newline, ``\\nhvs.``, or a URL-encoded ``%3D``) is still masked in logs and errors. The
    # outgoing-content guard alone skips an ``s.``/``b.`` match whose body reads as an identifier (code member access);
    # never ``hvs.`` (ADR-0127; tests/unit/test_redaction_never_weaker.py keeps this pattern from getting weaker).
    ("vault_token_short", re.compile(r"(?:hvs|s|b)\.[A-Za-z0-9]{24,}")),
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


# Matches of these kinds that are ordinary code or encoded data, not a credential (ADR-0127, 2026-10-28). Only the
# high-confidence scan (the broker's outgoing guard, contains_high_confidence_secret) skips them: redaction of logs and
# errors keeps masking the wider shape, and the binary scan keeps the unbounded form.
_CODE_OR_DATA_LOOKALIKES = frozenset({"vault_token_short", "openbao_recovery_token", "google_refresh_token",
                                      "google_api_key"})
# Characters of standard and URL-safe base64. A Google key or refresh token glued to one of them sits inside a base64
# run (an image data URI, a lockfile ``sha512-`` integrity hash), where ``AIza`` or ``1//`` occur by chance.
_BASE64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-")
_IDENTIFIER_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_IDENTIFIER_MEAN_WORD_LENGTH = 3.4


def _reads_as_identifier(body: str) -> bool:
    """True when *body* splits into camelCase or snake_case words of mean length 3.4 or more, as a member or variable
    name does (registerServiceWithHealthCheck 6.0, isInCurrentRuleReSyncSet 3.4; the shortest of 3,909 long member
    names measured in the frontend and Python dependencies). A random token body falls apart into one- and
    two-character pieces: about 0.5% of random 24-character bodies, 0.015% of 40-character ones and none of 100
    reach 3.4 (measured 2026-10-28 on 200,000, 400,000 and 100,000 random bodies; the ADR-0127 residual). Linear in
    the length of *body*."""
    words = _IDENTIFIER_WORD.findall(body)
    return bool(words) and sum(map(len, words)) >= _IDENTIFIER_MEAN_WORD_LENGTH * len(words)


# An escape sequence in front of (or after) a token is a boundary, not part of a word (ADR-0127 decision 7). One
# general rule, not a list of contexts: every escape introducer and every escape sequence is blanked, in any of the
# forms text takes on its way into a model request:
# * raw control bytes: ESC, BEL, NUL and every other C0 or C1 control character (a tab, a newline and a carriage return
#   are whitespace already);
# * a backslash escape, the backslash raw or URL-encoded (``%5C``, ``%255C``) and any run of them (JSON encoded once or
#   several times): JSON (``\n``, ``\t``, ``\r``, ``\"``, ``\\``, ``\/``, ``\b``, ``\f``, ``\u00e9``), C, Python and shell
#   (``\a``, ``\e``, ``\v``, ``\xNN``, ``\NNN`` octal, ``\0``, ``\U0000001b``, ``\u{7}``);
# * an ESC in any of these forms (raw, ``\u001b``, ``\x1b``, ``\033``, ``\e``, ``\u{1b}``, caret ``^[``, ``%1B``) with
#   what follows it: a CSI sequence (``[`` or ``%5B``, parameter bytes, intermediate bytes, a final byte: ``ESC[31m``,
#   ``ESC[m``), the head of an OSC sequence (``]`` or ``%5D`` with its number and ``;``: ``ESC]8;;``) or another
#   escape (``ESC(B``, ``ESC7``); the OSC text itself (a hyperlink's URL, a window title) stays visible, since a token
#   can sit in it, and its terminator (BEL, or ESC + backslash) is an escape of its own;
# * a URL-encoded byte (``%3D``, ``%2F``, ``%25`` for encoded twice).
# No token pattern holds a backslash, ``%``, ``^`` or a control character, so blanking those never cuts a token. What can be
# ambiguous is the variable tail of an escape: a single-letter escape, the digits of an octal escape, the
# parameter, intermediate and final bytes of a CSI sequence, the number of an OSC sequence or the byte after an ESC.
# Read greedily, ``\e[`` followed by a token takes the token's first letter as the CSI final byte, ``\0`` followed by a
# Google refresh token takes its ``1`` as an octal digit, and a backslash pair followed by a batch token ``b.`` reads as
# the JSON escape ``\b``; read without the tail, ``ESC[31m`` followed by a Google key leaves the ``m`` glued to it. So the
# guard reads the text twice, and a token found in either reading is found: the greedy view blanks every escape
# whole; the cut view blanks each escape only up to the first character of its tail that could begin a token
# (_TOKEN_START), and keeps the rest. Both views have the length of the text, so a span found in either is the same
# span of the text. Each escape is matched from its first character and every alternative that starts a run of
# backslashes always succeeds, possessively, so a long run of escapes costs linear time, not quadratic.
_PERCENT = r"%(?:25)*"  # a percent sign, URL-encoded any number of times
_HEX = r"[0-9A-Fa-f]"


def _raw_or_encoded(raw: str, code: str) -> str:
    """A character, raw or URL-encoded any number of times (*code*: the regex of its two hex digits)."""
    return r"(?:" + raw + r"|" + _PERCENT + code + r")"


_BACKSLASH = _raw_or_encoded(r"\\", "5[cC]")
_LBRACKET = _raw_or_encoded(r"\[", "5[bB]")
_RBRACKET = _raw_or_encoded(r"\]", "5[dD]")
_LBRACE = _raw_or_encoded(r"\{", "7[bB]")
_RBRACE = _raw_or_encoded(r"\}", "7[dD]")
_PARAMETER = _raw_or_encoded(r"[0-?]", "3" + _HEX)  # a CSI parameter byte, 0x30-0x3F
# an intermediate byte, 0x20-0x2F, except ``%`` (0x25): a percent sign starts an encoded byte
_INTERMEDIATE = _raw_or_encoded(r"[ -$&-/]", "2[0-46-9A-Fa-f]")
_ESC_FORM = (r"(?:\x1b|" + _PERCENT + r"1[bB]|" + _raw_or_encoded(r"\^", "5[eE]") + _LBRACKET + r"|"
             + _BACKSLASH + r"++(?:[eE]|x1[bB]|0?33|u001[bB]|U0000001[bB]|u" + _LBRACE + r"0*1[bB]" + _RBRACE + r"))")
# Final bytes exclude ``\`` (a string terminator's backslash is an escape of its own) and ``^`` (a caret ESC ``^[``).
_ESCAPE_BOUNDARY = re.compile(
    _ESC_FORM + r"(?:" + _LBRACKET + r"(?P<csi>" + _PARAMETER + r"*+" + _INTERMEDIATE + r"*+[@-\[\]_-~]?)"
    r"|" + _RBRACKET + r"(?P<osc>[0-9]*+;*+)"
    r"|(?P<esc>" + _INTERMEDIATE + r"*+[0-\[\]_-~]?))"
    r"|" + _BACKSLASH + r"++(?:u" + _HEX + r"{4}|U" + _HEX + r"{8}|u" + _LBRACE + _HEX + r"{1,6}" + _RBRACE
    + r"|x" + _HEX + r"{2}|(?P<oct>[0-7]{1,3})|(?P<letter>[abefnrtv\"'/?])|)"
    r"|" + _PERCENT + _HEX + r"{2}"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TAIL_GROUPS = ("csi", "osc", "esc", "oct", "letter")
# Every character a high-confidence pattern's match can begin with (sk-, s., b., r., gh, github_pat_, AKIA, AIza,
# ya29., 1//, hv, xox, the labels unseal / recovery / initial in any case, the quote of the `operator init` JSON, the
# dashes of a PEM header). tests/unit/test_redaction_never_weaker.py keeps it complete: every corpus token begins with
# one of these, and the grammar fuzz finds every corpus token after every escape it generates.
_TOKEN_START = frozenset('"-1AIRUbghirsuxy')
_TOKEN_START_RE = re.compile("[" + re.escape("".join(sorted(_TOKEN_START))) + "]")
_NEEDS_VIEW = re.compile(r"[\\%\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\^\[")


def _boundary_views(text: str) -> tuple[str, str | None]:
    """The greedy view of *text* (every escape of _ESCAPE_BOUNDARY replaced by as many spaces) and the cut view (each
    escape blanked only up to the first character of its tail in _TOKEN_START), or None for the cut view when it would
    equal the greedy one. Both have the length of *text*. Linear."""
    if not _NEEDS_VIEW.search(text):
        return text, None
    greedy: list[str] = []
    cut: list[str] = []
    differ = False
    pos = 0
    for m in _ESCAPE_BOUNDARY.finditer(text):
        start, end = m.span()
        keep = end
        for name in _TAIL_GROUPS:
            tail = m.start(name)
            if tail != -1:
                first = _TOKEN_START_RE.search(text, tail, end)
                keep = first.start() if first else end
                break
        between = text[pos:start]
        greedy += (between, " " * (end - start))
        cut += (between, " " * (keep - start), text[keep:end])
        differ = differ or keep != end
        pos = end
    greedy.append(text[pos:])
    cut.append(text[pos:])
    return "".join(greedy), ("".join(cut) if differ else None)


def _glued_to_base64(text: str, start: int, end: int) -> bool:
    return (start > 0 and text[start - 1] in _BASE64_CHARS) or (end < len(text) and text[end] in _BASE64_CHARS)


def _is_code_or_data(kind: str, text: str, start: int, end: int) -> bool:
    """A match of a _CODE_OR_DATA_LOOKALIKES kind that is not a credential: ``s.registerServiceWithHealthCheck`` (a Go
    receiver ``s``, ``b``, ``r``), ``total1//itemsPerPage`` (floor division), ``AIza``/``1//`` inside base64."""
    if kind == "google_api_key":
        return _glued_to_base64(text, start, end)
    if kind == "google_refresh_token":
        return _glued_to_base64(text, start, end) or _reads_as_identifier(text[start + 3:end])
    dot = text.index(".", start, end)  # vault_token_short, openbao_recovery_token: <prefix>.<body>
    return text[start:dot] != "hvs" and _reads_as_identifier(text[dot + 1:end])


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
    # The token patterns run on the boundary views (same length, same spans): a token after an escaped newline or an
    # encoded ``=`` is found, and its span still names the token in *text*. A token the cut view alone finds (its first
    # letter read as the tail of an escape in the greedy view) is added unless a finding of the greedy view overlaps it.
    greedy, cut = _boundary_views(text)
    found: list[SecretFinding] = []
    for finding in _scan_view(greedy):
        found.append(finding)
        yield finding
    if cut is None:
        return
    found.sort(key=lambda f: f.start)
    starts = [f.start for f in found]
    reach = list(accumulate((f.end for f in found), max))
    for finding in _scan_view(cut):
        i = bisect_left(starts, finding.end)
        if i == 0 or reach[i - 1] <= finding.start:
            yield finding


def _scan_view(text: str) -> Iterator[SecretFinding]:
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
            if kind in _CODE_OR_DATA_LOOKALIKES and _is_code_or_data(kind, text, m.start(), m.end()):
                continue
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
