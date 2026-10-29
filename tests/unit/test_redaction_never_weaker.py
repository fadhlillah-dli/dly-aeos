"""Secret redaction never gets weaker (LESSONS L25, ADR-0127; secret class, so the tool is due at strike 1).

Incident (2026-10-28): a change to the secret patterns made for the broker's outgoing-content guard added a
look-behind to the short Vault token pattern, and log redaction stopped masking a token glued to a letter, such as
one after a JSON-escaped newline (``"$ cat ~/.vault-token\\nhvs...."`` in a worker log). A blind differential review
caught it before merge.

This test is that differential, kept: a corpus of secret shapes, one or more variants for every pattern of
``aeos.core.redaction`` as origin/main defined them when the incident was found (each body holds every character its
pattern's class allows; long bodies too), placed in many contexts (plain, after a letter or digit, after a JSON escape,
after a URL-encoded byte, after a terminal colour code raw or written as text, after a bare textual ESC + ``[``, after
a textual BEL or NUL, after an OSC-8 hyperlink terminator, inside a data URI, inside a JSON string, a multi-line PEM
block, the indented `operator init -format=json` output). It asserts that

* ``redact_text`` masks completely (no piece of ``PIECE`` characters of any part of the secret survives: every key of
  an ``operator init`` JSON list, every line of a PEM body) and ``contains_secret``
  detects every (variant, context) that origin/main caught: the only pairs it may miss are listed in ``MAIN_MISSES``
  (derived by running origin/main's patterns over this corpus); a check of the whole value alone stayed green when a
  narrowed character class masked only a prefix, and a check of the first key or line alone stayed green when a change
  narrowed only the repeated group of a pattern (reviews of 2026-10-28);
* the outgoing guard (``find_high_confidence_secrets`` and the broker's ``sensitive_content_findings``) finds every
  token-shaped item in every context except the documented base64 exception (a Google key or refresh token glued to
  a letter or digit reads as base64 data), including after escapes origin/main's redaction misses;
* near misses (code identifiers, base64 image data, ``sha512-`` lockfile integrity hashes, also JSON-encoded) are not
  flagged by the guard.

tests/unit/test_redaction_escape_fuzz.py places every corpus secret after 5,000 escape prefixes generated from a
grammar, with the same two assertions: the contexts here are the named regression cases, the fuzz finds the next one.

A pattern change that drops a case is red with a message naming the pattern (detector kind) and the context, never
the value (LESSONS L26: no report quotes a secret shape, synthetic ones included). A new pattern needs a corpus variant
here (the coverage test names it). Every sample is assembled at run time from prefixes split into parts and seeded
random bodies, so this file holds no literal secret shape and the repository secret scan does not flag it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
import urllib.parse
from collections.abc import Callable

import pytest

from aeos.core import redaction
from aeos.core.redaction import contains_secret, find_high_confidence_secrets, redact_text

# The detector kinds of origin/main's patterns when the incident was found (in order). The corpus covers each.
MAIN_KINDS = (
    "openrouter_key", "anthropic_key", "openai_style_key", "github_token", "github_pat", "aws_access_key_id",
    "google_api_key", "google_oauth_token", "google_refresh_token", "vault_token", "vault_token_short",
    "openbao_recovery_token", "vault_unseal_key", "vault_root_token", "vault_init_json", "slack_token",
    "private_key_pem", "bearer_value", "credential_assignment",
)
# key=value forms (a capture group): redacted in logs, never a reason for the guard to refuse (ADR-0127)
GROUP_KINDS = frozenset({"bearer_value", "credential_assignment"})

# Every corpus value is assembled at run time from a seeded random draw over the characters its origin/main pattern
# allows (LESSONS L9, L26): the file holds no literal secret shape, and no report needs to quote one. A body starts with
# 24 letters and digits, then holds every character of its class in shuffled order, then ends with 10 letters and
# digits: a pattern whose class loses a character masks at most the part before that character and leaves at least 10
# characters unmasked, which the 8-character-piece check below sees (the review of 2026-10-28: a check of the whole
# value only stays green when a narrowed class masks just a prefix).
_RNG = random.Random(20261028)
_ALNUM = string.ascii_letters + string.digits
_UPPER_DIGITS = string.ascii_uppercase + string.digits
_LONG = 400  # a long body: the whole of it must be masked, not a prefix of bounded length
# The characters of ``[^\s,;"']`` that JSON encodes unchanged (a backslash would be doubled): the credential value class
_ASSIGNMENT_CLASS = _ALNUM + "!#$%&()*+-./:<=>?@[]^_`{|}~"


def _draw(chars: str, n: int) -> str:
    return "".join(_RNG.choice(chars) for _ in range(n))


def _body(extra: str = "", *, alnum: str = _ALNUM, length: int = 0) -> str:
    """24 of *alnum*, every character of ``alnum + extra`` once in shuffled order (repeated up to *length*), 10 of
    *alnum*."""
    middle = ""
    while not middle or len(middle) + 34 < length:
        middle += "".join(_RNG.sample(alnum + extra, len(alnum + extra)))
    return _draw(alnum, 24) + middle + _draw(alnum, 10)


def _fixed(chars: str, size: int) -> list[str]:
    """Bodies of exactly *size* characters that together hold every character of *chars* (a fixed-length class)."""
    pool = "".join(_RNG.sample(chars, len(chars)))
    return [(pool[i:i + size] + _draw(chars, size))[:size] for i in range(0, len(pool), size)]


def _pem(kind: str) -> tuple[str, tuple[str, ...]]:
    b64 = _ALNUM + "+/"
    lines = ["".join(_RNG.sample(b64, 64)) for _ in range(3)] + [_draw(b64, 20) + "=="]
    item = ("-----BEGIN " + kind + "PRIVATE " + "KEY-----\n" + "\n".join(lines) + "\n-----END " + kind + "PRIVATE "
            + "KEY-----")
    return item, tuple(lines)


def _variants() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(variant id, detector kind, item placed in a context, the parts of the secret: no piece of any part may survive
    redaction). A multi-part secret lists every part (each key of an `operator init` JSON list, every line of a PEM
    body): origin/main masks the repeated part of its pattern too, and a check of the first part alone stays green
    when a change narrows only the repeated part (review of 2026-10-28)."""
    out: list[tuple[str, str, str, tuple[str, ...]]] = []

    def add(vid: str, kind: str, item: str, value: str | tuple[str, ...] | None = None) -> None:
        parts = (item,) if value is None else (value,) if isinstance(value, str) else value
        out.append((vid, kind, item, parts))

    def token(vid: str, kind: str, prefix: str, extra: str = "", alnum: str = _ALNUM) -> None:
        add(vid, kind, prefix + _body(extra, alnum=alnum))
        add(vid + "_long", kind, prefix + _body(extra, alnum=alnum, length=_LONG))

    sk = "s" + "k-"
    token("openrouter", "openrouter_key", sk + "or-" + "v1-")
    token("anthropic", "anthropic_key", sk + "ant-" + "api03-", "_-")
    token("openai_project", "openai_style_key", sk + "proj-", "_-")
    for c in "pousr":
        token(f"github_gh{c}", "github_token", "g" + "h" + c + "_")
    token("github_pat", "github_pat", "github" + "_pat_", "_")
    for i, body in enumerate(_fixed(_UPPER_DIGITS, 16), 1):
        add(f"aws_key_id_{i}", "aws_access_key_id", "AK" + "IA" + body)
    for i, body in enumerate(_fixed(_ALNUM + "_-", 35), 1):
        add(f"google_api_key_{i}", "google_api_key", "AI" + "za" + body)
    token("google_oauth", "google_oauth_token", "ya" + "29.", "_-")
    token("google_refresh", "google_refresh_token", "1/" + "/", "_-")
    for c in "sbr":
        token(f"vault_hv{c}", "vault_token", "h" + "v" + c + ".", "_-")
    for c in "sb":
        token(f"vault_short_{c}", "vault_token_short", c + ".")
    token("openbao_recovery", "openbao_recovery_token", "r" + ".")
    key, key2, key3 = _body("+/") + "==", _body("+/"), _body("+/=")
    for label in ("Unseal Key 1", "Recovery Key 3", "unseal key share 2", "Unseal Key"):
        add("unseal_" + label.lower().replace(" ", "_"), "vault_unseal_key", label + ": " + key, key)
    long_key = _body("+/", length=_LONG) + "="
    add("unseal_long", "vault_unseal_key", "Unseal Key 2: " + long_key, long_key)
    root = _body("._-")
    add("root_token", "vault_root_token", "Initial Root Token: " + root, root)
    root_long = _body("._-", length=_LONG)
    add("root_token_long", "vault_root_token", "Initial Root Token: " + root_long, root_long)
    for which in ("unseal", "recovery"):
        for enc in ("b64", "hex"):
            add(f"init_json_{which}_{enc}", "vault_init_json",
                json.dumps({f"{which}_keys_{enc}": [key, key2, key3]}), (key, key2, key3))
    # `operator init -format=json` as the binary prints it: indented over several lines (review of 2026-10-28: with
    # single-line samples only, a pattern whose list separator or head lost its newline whitespace stayed green)
    hex_keys = [_draw("0123456789abcdef", 64) for _ in range(3)]
    init = {"unseal_keys_b64": [key, key2, key3], "unseal_keys_hex": hex_keys, "unseal_shares": 3,
            "unseal_threshold": 2, "recovery_keys_b64": [], "recovery_keys_hex": [], "recovery_keys_shares": 0,
            "recovery_keys_threshold": 0}
    for indent in (2, 4):
        for eol, newline in (("", "\n"), ("_crlf", "\r\n")):
            add(f"init_json_indent{indent}{eol}", "vault_init_json",
                json.dumps(init, indent=indent).replace("\n", newline), (key, key2, key3, *hex_keys))
    for c in "baprs":
        token(f"slack_xox{c}", "slack_token", "xo" + "x" + c + "-", "-")
    for label in ("", "RSA ", "EC ", "OPENSSH "):
        item, lines = _pem(label)
        add("pem_" + (label.strip().lower() or "pkcs8"), "private_key_pem", item, lines)
    for vid, length in (("bearer", 0), ("bearer_long", _LONG)):
        value = _body("._-~+/", length=length) + "=="
        add(vid, "bearer_value", "Authorization: Bearer " + value, value)
    for label in ("password=", "passwd: ", "secret=", "token: ", "api_key=", "api-key: ", "apikey="):
        value = _body(_ASSIGNMENT_CLASS[len(_ALNUM):])
        add("assign_" + re.sub(r"\W", "", label), "credential_assignment", label + value, value)
    value = _body(_ASSIGNMENT_CLASS[len(_ALNUM):], length=_LONG)
    add("assign_long", "credential_assignment", "password=" + value, value)
    return out


VARIANTS = _variants()

CONTEXTS: dict[str, Callable[[str], str]] = {
    "plain": lambda t: t,
    "after_space": lambda t: "value " + t + " end",
    "after_letter": lambda t: "x" + t,
    "after_digit": lambda t: "7" + t,
    "multiline": lambda t: "line one\n" + t + "\nline three",
    "json_string": lambda t: json.dumps({"value": t}),
    "after_json_newline": lambda t: json.dumps({"output": "$ cat ~/.config/token\n" + t}),
    "after_json_tab": lambda t: json.dumps({"o": "key\t" + t}),
    "after_json_quote": lambda t: json.dumps({"o": 'said "' + t + '"'}),
    "after_json_unicode": lambda t: json.dumps({"o": "café" + t}),
    "double_json_newline": lambda t: json.dumps(json.dumps({"o": "x\n" + t})),
    "after_url_eq": lambda t: "https://h.example/cb?state=1&token%3D" + t,
    "after_url_amp": lambda t: "https://h.example/cb?a%3D1%26" + t,
    "after_url_question": lambda t: "https://h.example/p%3F" + t,
    "after_url_slash": lambda t: "https://h.example/p%2F" + t,
    "after_url_at": lambda t: "https://user%40" + t + "@h.example/",
    "after_url_plus": lambda t: "q=a%2B" + t,
    "after_url_backslash": lambda t: "path=C%3A%5C" + t,
    "after_ansi_colour": lambda t: "\x1b[31m" + t + "\x1b[0m",
    "after_json_ansi_colour": lambda t: json.dumps({"log": "\x1b[1;32m" + t + "\x1b[0m"}),
    # a colour code written as text, as in shell source or a log of it: backslash x1b, backslash 033, backslash e
    "after_textual_hex_colour": lambda t: "echo -e '\\x1b[31m" + t + "\\x1b[0m'",
    "after_textual_octal_colour": lambda t: "printf '\\033[1;32m" + t + "\\033[0m\\n'",
    "after_textual_e_colour": lambda t: "printf '\\e[31m" + t + "\\e[0m'",
    # an ESC + ``[`` written as text with nothing after it (no parameter, no final byte of its own), textual BEL forms
    # and a textual NUL: the token's first letter or digit must not be read as part of the escape (review of 2026-10-28)
    "after_textual_csi_introducer": lambda t: "printf '\\e[" + t + "'",
    "after_textual_bel": lambda t: "printf 'x\\a" + t + "'",
    "after_textual_hex_bel": lambda t: "echo -e 'x\\x07" + t + "'",
    "after_textual_octal_bel": lambda t: "printf 'x\\007" + t + "'",
    "after_textual_nul": lambda t: "printf 'x\\0" + t + "'",
    # an OSC-8 terminal hyperlink: its terminator is ESC + backslash, raw, JSON-escaped or written as text
    "after_osc8_link": lambda t: "\x1b]8;;https://h.example/x\x1b\\" + t + "\x1b]8;;\x1b\\",
    "after_json_osc8_link": lambda t: json.dumps({"log": "\x1b]8;;https://h.example/x\x1b\\" + t + "\x1b]8;;\x1b\\"}),
    "after_textual_osc8_link": lambda t: "printf '\\e]8;;https://h.example/x\\e\\\\" + t + "\\e]8;;\\e\\\\'",
    "data_uri": lambda t: "data:text/plain;charset=utf-8," + t,
}
GLUED = frozenset({"after_letter", "after_digit"})

# (variant id, context) pairs origin/main's redact_text did NOT mask completely: a piece of PIECE characters survived
# (derived on 2026-10-28 by running origin/main's patterns over this corpus; the set equals that run exactly).
# Everything else must stay masked completely. Shrinking this set is fine; adding to it weakens redaction and needs an
# owner decision recorded in an ADR.
# Contexts in which the token directly follows a letter or digit: glued, or after an escape that ends in one
# (``\n``, ``\t``, ``\u00e9``, ``%3D``, ``%26``, ``%3F``, ``%2F``, ``%40``, ``%2B``, ``%5C``, a colour code ``ESC[31m``, raw or
# written as text, a textual BEL or NUL: ``\a``, ``\x07``, ``\007``, ``\0``). After an OSC-8 terminator (ESC + backslash)
# the token follows a backslash, and after a bare textual ESC + ``[`` a bracket: origin/main masks both.
_LETTER_OR_DIGIT_BEFORE = ("after_letter", "after_digit", "after_json_newline", "after_json_tab", "after_json_unicode",
                           "double_json_newline", "after_url_eq", "after_url_amp", "after_url_question",
                           "after_url_slash", "after_url_at", "after_url_plus", "after_url_backslash",
                           "after_ansi_colour", "after_json_ansi_colour", "after_textual_hex_colour",
                           "after_textual_octal_colour", "after_textual_e_colour", "after_textual_bel",
                           "after_textual_hex_bel", "after_textual_octal_bel", "after_textual_nul")
# Contexts in which JSON escaping turned the quotes of a labelled JSON shape into ``\"``.
_JSON_ENCODED = ("json_string", "after_json_newline", "after_json_tab", "after_json_quote", "after_json_unicode",
                 "double_json_newline", "after_json_ansi_colour", "after_json_osc8_link")
MAIN_MISSES = frozenset(
    # a look-behind (``sk-``, ``hv[sbr].``, ``r.``) or a word boundary (the unseal-key label) needs a non-alphanumeric
    # character before the shape; glued, an ``hvs.``/``hvb.`` token is masked by the short pattern only up to its
    # first ``_`` or ``-``, and the rest stays readable
    [(vid, ctx) for vid, kind, _, _ in VARIANTS
     if kind in ("openrouter_key", "anthropic_key", "openai_style_key", "vault_token", "openbao_recovery_token",
                 "vault_unseal_key")
     for ctx in _LETTER_OR_DIGIT_BEFORE]
    # the `operator init` JSON pattern needs literal quotes
    + [(vid, ctx) for vid, kind, _, _ in VARIANTS if kind == "vault_init_json" for ctx in _JSON_ENCODED])
# The guard's documented exception (ADR-0127 decision 7): a Google key or refresh token glued to a letter or digit
# reads as part of a base64 run (an image data URI, a lockfile hash), where these prefixes occur by chance.
GUARD_EXEMPT = frozenset((vid, ctx) for vid, kind, _, _ in VARIANTS
                         if kind in ("google_api_key", "google_refresh_token") for ctx in GLUED)
# Shapes the guard does not see either, all in MAIN_MISSES: a pattern that needs a boundary before it, written directly
# after a letter or digit (a word, not an escape), and the labelled `operator init` JSON once JSON escaping has turned
# the quotes its label needs into ``\"``; and the root-token label glued to a word, which only the generic
# ``token: value`` rule masks in logs (a key=value rule, never a guard rule). After an escape sequence the guard finds
# every other shape.
GUARD_UNSEEN = frozenset((vid, ctx) for vid, kind, _, _ in VARIANTS for ctx in CONTEXTS
                         if ((vid, ctx) in MAIN_MISSES and (ctx in GLUED or kind == "vault_init_json"))
                         or (kind == "vault_root_token" and ctx in GLUED))


def _cases() -> list[tuple[str, str, str, str, tuple[str, ...]]]:
    return [(vid, kind, ctx, wrap(item), parts) for vid, kind, item, parts in VARIANTS for ctx, wrap in CONTEXTS.items()]


def _what(kind: str, vid: str, ctx: str) -> str:
    return f"pattern {kind} (variant {vid}) in context {ctx}"


PIECE = 8  # no piece of a secret this long may survive redaction


def _pieces(parts: tuple[str, ...]) -> list[str]:
    """Every PIECE-character piece of every part of a secret (each key of a list, every line of a PEM body)."""
    return [part[i:i + PIECE] for part in parts for i in range(len(part) - PIECE + 1)]


def unmasked_pieces(parts: tuple[str, ...], redacted: str) -> int:
    """How many of the PIECE-character pieces of the secret's *parts* are still in *redacted*: 0 when all of every part
    is masked. A check of the whole value alone stays green when a narrowed pattern masks only a prefix, and a check of
    the first part alone when a change narrows only a repeated part (reviews of 2026-10-28)."""
    return sum(piece in redacted for piece in _pieces(parts))


def redaction_regressions() -> list[str]:
    """Every (variant, context) origin/main masked completely that redact_text no longer masks completely, or that
    contains_secret no longer detects. The message names the pattern and the context, never the value (L26)."""
    out = []
    for vid, kind, ctx, text, parts in _cases():
        if (vid, ctx) in MAIN_MISSES:
            continue
        left = unmasked_pieces(parts, redact_text(text))
        if left:
            out.append(f"{_what(kind, vid, ctx)}: redact_text leaves {left} of its {len(_pieces(parts))} "
                       f"{PIECE}-character pieces unmasked (origin/main masked all)")
        if not contains_secret(text):
            out.append(f"{_what(kind, vid, ctx)}: contains_secret no longer detects it (origin/main did)")
    return out


def _overlaps(text: str, value: str, spans: list[tuple[int, int]]) -> bool:
    i = text.find(value)
    return i != -1 and any(s < i + len(value) and e > i for s, e in spans)


def guard_misses() -> list[str]:
    """Every token-shaped (variant, context) the outgoing guard does not find, outside its documented exceptions."""
    out = []
    for vid, kind, ctx, text, parts in _cases():
        if kind in GROUP_KINDS or (vid, ctx) in GUARD_EXEMPT or (vid, ctx) in GUARD_UNSEEN:
            continue
        spans = [(f.start, f.end) for f in find_high_confidence_secrets(text)]
        if not _overlaps(text, parts[0], spans):
            out.append(f"{_what(kind, vid, ctx)}: the outgoing guard does not find it")
    return out


# ------------------------------------------------------------------------------------------------- the differential
def test_the_corpus_covers_every_pattern_of_origin_main_and_of_today():
    kinds = {kind for _, kind, _, _ in VARIANTS}
    today = [kind for kind, _ in redaction._NAMED_PATTERNS]
    assert set(MAIN_KINDS) <= kinds, f"corpus lost a pattern of origin/main: {sorted(set(MAIN_KINDS) - kinds)}"
    missing = [k for k in today if k not in kinds]
    assert not missing, f"pattern(s) {missing} have no corpus variant here: add realistic samples and contexts"
    gone = [k for k in MAIN_KINDS if k not in today]
    assert not gone, f"pattern(s) {gone} of origin/main no longer exist: redaction would get weaker"


def test_each_variant_is_still_matched_whole_by_its_own_pattern():
    """Each part of each variant's secret lies inside a match of its own pattern: the whole body, every character of its
    class, every key of a list and every line of a PEM body."""
    patterns = dict(redaction._NAMED_PATTERNS)
    wrong = [f"pattern {kind} no longer matches the whole of its variant {vid} (plain, part {n})"
             for vid, kind, item, parts in VARIANTS for n, value in enumerate(parts, 1)
             if not any(m.start() <= item.find(value) and item.find(value) + len(value) <= m.end()
                        for m in patterns[kind].finditer(item))]
    assert not wrong, "\n".join(wrong)


def test_redaction_masks_everything_origin_main_masked():
    regressions = redaction_regressions()
    assert not regressions, ("secret redaction got weaker (LESSONS L25):\n" + "\n".join(regressions))


def test_the_outgoing_guard_finds_every_token_in_every_context():
    misses = guard_misses()
    assert not misses, ("the outgoing guard misses a credential (ADR-0127):\n" + "\n".join(misses))


def test_the_broker_refuses_every_token_after_a_json_escape_in_a_tool_result():
    from aeos.broker.model_proxy import sensitive_content_findings

    escaped = ("after_json_newline", "after_json_tab", "after_json_quote", "after_json_unicode", "double_json_newline",
               "after_json_ansi_colour", "after_json_osc8_link")
    missed = []
    for vid, kind, ctx, text, _ in _cases():
        if ctx not in escaped or kind in GROUP_KINDS or (vid, ctx) in GUARD_UNSEEN:
            continue
        if not sensitive_content_findings({"messages": [{"role": "tool", "content": text}]}):
            missed.append(_what(kind, vid, ctx))
    assert not missed, "a tool result carrying a credential would be sent:\n" + "\n".join(missed)


# ------------------------------------------------------------------------------------------------- near misses
def _image_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(random.Random(2).randbytes(750_000)).decode()


def _lockfile(entries: int) -> str:
    lines = (f'    "node_modules/p{i}": {{"integrity": "sha512-'
             + base64.b64encode(hashlib.sha512(str(i).encode()).digest()).decode() + '"},' for i in range(entries))
    return "{\n" + "\n".join(lines) + "\n}"


def _url_encoded_base64() -> str:
    """Base64 data, URL-encoded, with a Google-key lookalike right after an encoded ``/`` and base64 on its right: the
    ``%2F`` is blanked as a boundary, and the lookalike still reads as glued to the base64 run after it."""
    rng = random.Random(3)
    data = base64.b64encode(rng.randbytes(3000)).decode()
    planted = data[:1000] + "/" + "AI" + "za" + "".join(rng.choice(_ALNUM) for _ in range(36)) + data[1000:]
    return "img=" + urllib.parse.quote(planted, safe="")


NEAR_MISSES = {
    "js_members": "this" + ".handleSubmitButtonClickedEventNow(); lib" + ".someLongFunctionNameForTheParser();",
    "go_receivers": ("return s" + ".registerServiceWithHealthCheck(ctx)\n b" + ".ReportAllocsAndResetTimerNow()\n"
                     "rows, err := r" + ".findAllByOrganizationIdentifier(id)\n"),
    "python_model_member": "see model" + "s" + ".CustomerSubscriptionHistoryRecord",
    "floor_division": "pages = total1/" + "/itemsPerPageForThisListing",
    "image_data_uri": _image_data_uri(),
    "lockfile_sha512": _lockfile(5_000),
    "ansi_coloured_code": "\x1b[32m" + "s" + ".registerServiceWithHealthCheck(ctx)\x1b[0m r" + ".findAllByOrganizationId()",
    "url_encoded_base64": _url_encoded_base64(),
    "textual_colour_code": ("printf '\\e[32m" + "s" + ".registerServiceWithHealthCheck(ctx)\\x1b[0m' \\033[1m"
                            + "b" + ".ReportAllocsAndResetTimerNow()"),
    "osc8_linked_code": ("\x1b]8;;file:///src/a.go\x1b\\" + "r" + ".findAllByOrganizationIdentifier(id)\x1b]8;;\x1b\\ "
                         "\\e]8;;x\\e\\\\" + "b" + ".ReportAllocsAndResetTimerNow()"),
}


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
@pytest.mark.parametrize("encoding", ["raw", "json", "json_twice"])
def test_near_misses_are_not_flagged_by_the_guard(name, encoding):
    text = NEAR_MISSES[name]
    text = {"raw": text, "json": json.dumps({"output": text}),
            "json_twice": json.dumps(json.dumps({"output": text}))}[encoding]
    found = sorted({f.kind for f in find_high_confidence_secrets(text)})
    assert found == [], f"near miss {name} ({encoding}) flagged as {found}"


# ------------------------------------------------------------------------------------------------- bite tests
def test_bite_the_incident_pattern_is_red_naming_the_pattern_and_the_context(monkeypatch):
    """The violating fixture: the short Vault pattern with the look-behind the incident added. The checker is red and
    its message names the pattern and the context (a token after a JSON-escaped newline)."""
    weakened = re.compile(r"(?<![A-Za-z0-9])(?:hvs|s|b)\.[A-Za-z0-9]{24,}")
    named = [(k, weakened if k == "vault_token_short" else p) for k, p in redaction._NAMED_PATTERNS]
    monkeypatch.setattr(redaction, "_NAMED_PATTERNS", named)
    monkeypatch.setattr(redaction, "_PATTERNS", [p for _, p in named])
    regressions = redaction_regressions()
    assert any(r.startswith("pattern vault_token_short (variant vault_short_s) in context after_letter: redact_text "
                            "leaves ") for r in regressions), regressions
    assert any("vault_token_short" in r and "after_json_newline" in r for r in regressions), regressions


def test_bite_a_dropped_pattern_is_red_and_the_unchanged_patterns_are_green(monkeypatch):
    assert redaction_regressions() == [] and guard_misses() == []  # the near miss: today's patterns
    named = [(k, p) for k, p in redaction._NAMED_PATTERNS if k != "slack_token"]
    monkeypatch.setattr(redaction, "_NAMED_PATTERNS", named)
    monkeypatch.setattr(redaction, "_PATTERNS", [p for _, p in named])
    assert any(r.startswith("pattern slack_token (variant slack_xoxb) in context plain: redact_text leaves ")
               for r in redaction_regressions())
    assert "pattern slack_token (variant slack_xoxb) in context data_uri: the outgoing guard does not find it" \
           in guard_misses()


def test_bite_a_narrowed_character_class_that_masks_only_a_prefix_is_red(monkeypatch):
    """Review of 2026-10-28: the Google OAuth pattern loses ``_`` from its class. It still masks the body up to the first
    ``_`` and the rest stays readable: a check of the whole value is green (the near miss below), the piece check is
    red and names the pattern and the context."""
    narrowed = re.compile(r"ya29\.[0-9A-Za-z\-]{20,}")
    named = [(k, narrowed if k == "google_oauth_token" else p) for k, p in redaction._NAMED_PATTERNS]
    monkeypatch.setattr(redaction, "_NAMED_PATTERNS", named)
    monkeypatch.setattr(redaction, "_PATTERNS", [p for _, p in named])
    (text, (value,)), = [(item, parts) for vid, _, item, parts in VARIANTS if vid == "google_oauth"]
    redacted = redact_text(text)
    assert value not in redacted and redacted != text  # the old whole-value check stays green: that is the gap
    assert unmasked_pieces((value,), redacted) >= 3
    regressions = redaction_regressions()
    for ctx in ("plain", "after_json_newline", "after_ansi_colour"):
        assert any(r.startswith(f"pattern google_oauth_token (variant google_oauth) in context {ctx}: redact_text "
                                "leaves ") for r in regressions), (ctx, regressions)
    assert all("google_oauth" in r for r in regressions), regressions  # no other pattern is blamed
    assert not any(value[i:i + PIECE] in r for r in regressions for i in range(len(value) - PIECE + 1))  # L26


def _replace_pattern(monkeypatch, kind: str, pattern: re.Pattern[str]) -> None:
    named = [(k, pattern if k == kind else p) for k, p in redaction._NAMED_PATTERNS]
    monkeypatch.setattr(redaction, "_NAMED_PATTERNS", named)
    monkeypatch.setattr(redaction, "_PATTERNS", [p for _, p in named])


_INIT_JSON_REPEAT = r'(?:\s*,\s*"[A-Za-z0-9+/=]{32,}")*'  # the repeated group of origin/main's `operator init` pattern
_INIT_JSON_REPEAT_NARROWED = {
    "repeat_drops_plus": r'(?:\s*,\s*"[A-Za-z0-9/=]{32,}")*',
    "repeat_bounded_to_one": r'(?:\s*,\s*"[A-Za-z0-9+/=]{32,}"){0,1}',
}


@pytest.mark.parametrize("mutation", sorted(_INIT_JSON_REPEAT_NARROWED))
def test_bite_a_narrowed_repeated_group_of_the_init_json_list_is_red(monkeypatch, mutation):
    """Review of 2026-10-28: a change to only the repeated group of the `operator init` JSON pattern (its character
    class loses ``+``, or the repeat is bounded) leaves a later unseal key of the list whole in the log while the first
    key stays masked: a check of the first key alone is green (the gap), the check of every key is red and names the
    pattern and the context."""
    original = dict(redaction._NAMED_PATTERNS)["vault_init_json"]
    assert original.pattern.endswith(_INIT_JSON_REPEAT)
    head = original.pattern[:-len(_INIT_JSON_REPEAT)]
    _replace_pattern(monkeypatch, "vault_init_json", re.compile(head + _INIT_JSON_REPEAT_NARROWED[mutation]))
    (text, parts), = [(item, parts) for vid, _, item, parts in VARIANTS if vid == "init_json_unseal_b64"]
    redacted = redact_text(text)
    assert unmasked_pieces(parts[:1], redacted) == 0  # the first key is masked: a first-part check stays green
    assert unmasked_pieces(parts[1:], redacted) > 0
    regressions = redaction_regressions()
    for ctx in ("plain", "after_space", "multiline", "after_osc8_link"):
        assert any(r.startswith(f"pattern vault_init_json (variant init_json_unseal_b64) in context {ctx}: redact_text "
                                "leaves ") for r in regressions), (ctx, regressions)
    assert all(r.startswith("pattern vault_init_json ") for r in regressions), regressions  # no other pattern blamed
    pieces = _pieces(parts)
    assert not any(piece in r for r in regressions for piece in pieces)  # L26


def test_bite_a_pem_pattern_that_masks_only_the_first_body_line_is_red(monkeypatch):
    """The same gap for a PEM block: a pattern that masks the header and the first body line leaves the other lines
    readable; checking every body line is red and names the pattern and the context."""
    _replace_pattern(monkeypatch, "private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\n[^\n]*"))
    (text, parts), = [(item, parts) for vid, _, item, parts in VARIANTS if vid == "pem_rsa"]
    redacted = redact_text(text)
    assert unmasked_pieces(parts[:1], redacted) == 0 and unmasked_pieces(parts[1:], redacted) > 0
    regressions = redaction_regressions()
    assert any(r.startswith("pattern private_key_pem (variant pem_rsa) in context plain: redact_text leaves ")
               for r in regressions), regressions
    assert all(r.startswith("pattern private_key_pem ") for r in regressions), regressions


_CSI = r"\[[0-?]*[ -/]*[@-~]"
# The escape boundary of 787626a: raw and JSON-escaped colour codes only.
_BOUNDARY_787626A = re.compile(r"(?<!\\)\\++(?:u001[bB]" + _CSI + r"|u[0-9A-Fa-f]{4}|[nrtbf\"'/])"
                               r"|\x1b(?:" + _CSI + r")?"
                               r"|%(?:25)*[0-9A-Fa-f]{2}")
# The escape boundary of 528d93f: textual colour codes and the OSC-8 terminator too, one greedy view.
_BOUNDARY_528D93F = re.compile(r"(?<!\\)\\++(?:(?:u001[bB]|x1[bB]|033|e)(?:" + _CSI + r"|(?:\\\\)++)"
                               r"|u001[bB]|u[0-9A-Fa-f]{4}|[nrtbf\"'/])"
                               r"|\x1b(?:" + _CSI + r"|\\)?"
                               r"|%(?:25)*[0-9A-Fa-f]{2}")


def single_view(boundary: re.Pattern[str]) -> Callable[[str], tuple[str, None]]:
    """A boundary of an earlier build: one view in which every match of *boundary* is blanked."""
    return lambda text: (boundary.sub(lambda m: " " * (m.end() - m.start()), text), None)


_BOUNDARY_VIEWS = redaction._boundary_views


def greedy_view_only(text: str) -> tuple[str, None]:
    """Today's escape grammar, read greedily only (no cut view)."""
    return _BOUNDARY_VIEWS(text)[0], None


def cut_view_only(text: str) -> tuple[str, None]:
    """Today's escape grammar, read only up to the first tail character that could begin a token (no greedy view)."""
    greedy, cut = _BOUNDARY_VIEWS(text)
    return (greedy if cut is None else cut), None


def test_bite_textual_escapes_and_the_osc8_terminator_are_boundaries(monkeypatch):
    """Review of 2026-10-28: with a boundary that knows only raw and JSON-escaped colour codes, a token after a colour
    code written as text (backslash x1b, backslash 033, backslash e) reads as glued to ``m``, and after an OSC-8
    terminator (ESC + backslash) the backslash and the first letter of a ``b.`` or ``r.`` token read as the JSON escape
    backslash-b or backslash-r, and that letter is blanked: the guard is red, naming the pattern and the context.
    Today's boundary is the near miss (the guard test above is green, the code near misses stay unflagged)."""
    monkeypatch.setattr(redaction, "_boundary_views", single_view(_BOUNDARY_787626A))
    misses = guard_misses()
    for vid, kind in (("vault_short_b", "vault_token_short"), ("openbao_recovery", "openbao_recovery_token")):
        for ctx in ("after_osc8_link", "after_json_osc8_link", "after_textual_osc8_link"):
            assert f"pattern {kind} (variant {vid}) in context {ctx}: the outgoing guard does not find it" in misses, (
                vid, ctx)
    for ctx in ("after_textual_hex_colour", "after_textual_octal_colour", "after_textual_e_colour"):
        assert f"pattern anthropic_key (variant anthropic) in context {ctx}: the outgoing guard does not find it" \
            in misses, ctx


def test_bite_the_round_3_boundary_misses_tokens_after_textual_bel_and_a_bare_csi_introducer(monkeypatch):
    """Review of 2026-10-28 (HEAD 528d93f): its one greedy view blanked a textual ESC + ``[`` together with a CSI final
    byte, which was the first letter of the token after it, and did not know the textual BEL and NUL forms, so a token
    after them read as glued to ``a`` or ``7`` or ``0``: red, naming the pattern and the context."""
    monkeypatch.setattr(redaction, "_boundary_views", single_view(_BOUNDARY_528D93F))
    misses = guard_misses()
    for vid, kind in (("anthropic", "anthropic_key"), ("github_ghp", "github_token"), ("aws_key_id_1",
                                                                                       "aws_access_key_id")):
        assert f"pattern {kind} (variant {vid}) in context after_textual_csi_introducer: the outgoing guard does " \
               "not find it" in misses, vid
    for ctx in ("after_textual_bel", "after_textual_hex_bel", "after_textual_octal_bel", "after_textual_nul"):
        assert f"pattern anthropic_key (variant anthropic) in context {ctx}: the outgoing guard does not find it" \
            in misses, ctx


def test_bite_without_the_cut_view_a_token_read_as_an_escape_tail_is_missed(monkeypatch):
    """The greedy reading alone takes a token's first letter or digit as the tail of the escape before it (a CSI final
    byte after a bare ESC + ``[``, the letter after a backslash pair, an octal digit after ``\\0``): red. The cut view is
    what finds them (the near miss: today's two views, guard test above green)."""
    monkeypatch.setattr(redaction, "_boundary_views", greedy_view_only)
    misses = guard_misses()
    assert "pattern anthropic_key (variant anthropic) in context after_textual_csi_introducer: the outgoing guard " \
           "does not find it" in misses
    assert "pattern vault_token_short (variant vault_short_b) in context after_json_osc8_link: the outgoing guard " \
           "does not find it" in misses
    assert "pattern google_refresh_token (variant google_refresh) in context after_textual_nul: the outgoing guard " \
           "does not find it" in misses


def test_bite_without_the_greedy_view_a_token_after_a_colour_code_reads_as_glued(monkeypatch):
    """The cut reading alone keeps a colour code's parameter and final byte (``1m`` of ``ESC[31m``) when the parameter
    could begin a token, so a Google key after it reads as glued to base64: red. Both views together find it."""
    monkeypatch.setattr(redaction, "_boundary_views", cut_view_only)
    misses = guard_misses()
    assert "pattern google_api_key (variant google_api_key_1) in context after_ansi_colour: the outgoing guard " \
           "does not find it" in misses


def test_every_corpus_token_begins_with_a_token_start_character():
    """_TOKEN_START (the characters the cut view keeps) holds the first character of every guard finding of every
    token-shaped corpus variant: a new pattern whose matches begin with another character is red here."""
    outside = sorted({f"pattern {kind} (variant {vid})" for vid, kind, item, _ in VARIANTS if kind not in GROUP_KINDS
                      for f in find_high_confidence_secrets(item) if item[f.start] not in redaction._TOKEN_START})
    assert not outside, f"a token begins with a character outside _TOKEN_START: {outside}"
    assert all(any(True for _ in find_high_confidence_secrets(item)) for _, kind, item, _ in VARIANTS
               if kind not in GROUP_KINDS)


_INIT_JSON_WHITESPACE_NARROWED = {
    # the repeated group's separator, and the whitespace after the list's ``[``, lose the newline
    "separator_spaces_only": (r'(?:\s*,\s*"', r'(?: *, *"'),
    "head_spaces_only": (r'\[\s*"', r'\[ *"'),
}
_INDENTED = ("init_json_indent2", "init_json_indent4", "init_json_indent2_crlf", "init_json_indent4_crlf")


@pytest.mark.parametrize("mutation", sorted(_INIT_JSON_WHITESPACE_NARROWED))
def test_bite_narrowed_whitespace_in_the_init_json_pattern_is_red(monkeypatch, mutation):
    """Review of 2026-10-28: with single-line samples only, a pattern whose list separator (or the whitespace after
    ``[``) matches spaces but no newline stayed green while keys 2..n (or all keys) of the real, indented
    ``operator init -format=json`` output stayed readable in a log. The single-line variants are still masked whole (the
    gap); the indented ones are red, naming the pattern, the variant and the context, never the value."""
    original = dict(redaction._NAMED_PATTERNS)["vault_init_json"]
    old, new = _INIT_JSON_WHITESPACE_NARROWED[mutation]
    assert original.pattern.count(old) == 1
    _replace_pattern(monkeypatch, "vault_init_json", re.compile(original.pattern.replace(old, new)))
    for vid, _, item, parts in VARIANTS:
        if vid.startswith("init_json_") and vid not in _INDENTED:
            assert unmasked_pieces(parts, redact_text(item)) == 0, vid  # the single-line corpus stays green
    regressions = redaction_regressions()
    for vid in _INDENTED:
        for ctx in ("plain", "multiline", "after_space", "after_ansi_colour"):
            assert any(r.startswith(f"pattern vault_init_json (variant {vid}) in context {ctx}: redact_text leaves ")
                       for r in regressions), (vid, ctx, regressions)
    assert all(r.startswith("pattern vault_init_json (variant init_json_indent") for r in regressions), regressions
    pieces = {p for vid, _, _, parts in VARIANTS if vid in _INDENTED for p in _pieces(parts)}
    assert not any(piece in r for r in regressions for piece in pieces)  # L26
