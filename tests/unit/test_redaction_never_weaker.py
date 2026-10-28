"""Secret redaction never gets weaker (LESSONS L25, ADR-0127; secret class, so the tool is due at strike 1).

Incident (2026-10-28): a change to the secret patterns made for the broker's outgoing-content guard added a
look-behind to the short Vault token pattern, and log redaction stopped masking a token glued to a letter, such as
one after a JSON-escaped newline (``"$ cat ~/.vault-token\\nhvs...."`` in a worker log). A blind differential review
caught it before merge.

This test is that differential, kept: a corpus of secret shapes, one or more variants for every pattern of
``aeos.core.redaction`` as origin/main defined them when the incident was found (each body holds every character its
pattern's class allows; long bodies too), placed in many contexts (plain, after a letter or digit, after a JSON escape,
after a URL-encoded byte, after a terminal colour code, inside a data URI, inside a JSON string, a multi-line PEM
block). It asserts that

* ``redact_text`` masks completely (no piece of ``PIECE`` characters of the secret survives) and ``contains_secret``
  detects every (variant, context) that origin/main caught: the only pairs it may miss are listed in ``MAIN_MISSES``
  (derived by running origin/main's patterns over this corpus); a check of the whole value alone stayed green when a
  narrowed character class masked only a prefix (review of 2026-10-28);
* the outgoing guard (``find_high_confidence_secrets`` and the broker's ``sensitive_content_findings``) finds every
  token-shaped item in every context except the documented base64 exception (a Google key or refresh token glued to
  a letter or digit reads as base64 data), including after escapes origin/main's redaction misses;
* near misses (code identifiers, base64 image data, ``sha512-`` lockfile integrity hashes, also JSON-encoded) are not
  flagged by the guard.

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


def _pem(kind: str) -> tuple[str, str]:
    b64 = _ALNUM + "+/"
    lines = ["".join(_RNG.sample(b64, 64)) for _ in range(3)] + [_draw(b64, 20) + "=="]
    item = ("-----BEGIN " + kind + "PRIVATE " + "KEY-----\n" + "\n".join(lines) + "\n-----END " + kind + "PRIVATE "
            + "KEY-----")
    return item, lines[0]


def _variants() -> list[tuple[str, str, str, str]]:
    """(variant id, detector kind, item placed in a context, the secret value that must not survive redaction)."""
    out: list[tuple[str, str, str, str]] = []

    def add(vid: str, kind: str, item: str, value: str | None = None) -> None:
        out.append((vid, kind, item, item if value is None else value))

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
    key, key2 = _body("+/") + "==", _body("+/")
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
                json.dumps({f"{which}_keys_{enc}": [key, key2]}), key)
    for c in "baprs":
        token(f"slack_xox{c}", "slack_token", "xo" + "x" + c + "-", "-")
    for label in ("", "RSA ", "EC ", "OPENSSH "):
        item, value = _pem(label)
        add("pem_" + (label.strip().lower() or "pkcs8"), "private_key_pem", item, value)
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
    "data_uri": lambda t: "data:text/plain;charset=utf-8," + t,
}
GLUED = frozenset({"after_letter", "after_digit"})

# (variant id, context) pairs origin/main's redact_text did NOT mask completely: a piece of PIECE characters survived
# (derived on 2026-10-28 by running origin/main's patterns over this corpus; the set equals that run exactly).
# Everything else must stay masked completely. Shrinking this set is fine; adding to it weakens redaction and needs an
# owner decision recorded in an ADR.
# Contexts in which the token directly follows a letter or digit: glued, or after an escape that ends in one
# (``\n``, ``\t``, ``\u00e9``, ``%3D``, ``%26``, ``%3F``, ``%2F``, ``%40``, ``%2B``, ``%5C``, a colour code ``ESC[31m``).
_LETTER_OR_DIGIT_BEFORE = ("after_letter", "after_digit", "after_json_newline", "after_json_tab", "after_json_unicode",
                           "double_json_newline", "after_url_eq", "after_url_amp", "after_url_question",
                           "after_url_slash", "after_url_at", "after_url_plus", "after_url_backslash",
                           "after_ansi_colour", "after_json_ansi_colour")
# Contexts in which JSON escaping turned the quotes of a labelled JSON shape into ``\"``.
_JSON_ENCODED = ("json_string", "after_json_newline", "after_json_tab", "after_json_quote", "after_json_unicode",
                 "double_json_newline", "after_json_ansi_colour")
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


def _cases() -> list[tuple[str, str, str, str, str]]:
    return [(vid, kind, ctx, wrap(item), value) for vid, kind, item, value in VARIANTS for ctx, wrap in CONTEXTS.items()]


def _what(kind: str, vid: str, ctx: str) -> str:
    return f"pattern {kind} (variant {vid}) in context {ctx}"


PIECE = 8  # no piece of a secret this long may survive redaction


def unmasked_pieces(value: str, redacted: str) -> int:
    """How many of the PIECE-character pieces of *value* are still in *redacted*: 0 when all of it is masked. A check
    of the whole value alone stays green when a narrowed pattern masks only a prefix (review of 2026-10-28)."""
    return sum(value[i:i + PIECE] in redacted for i in range(len(value) - PIECE + 1))


def redaction_regressions() -> list[str]:
    """Every (variant, context) origin/main masked completely that redact_text no longer masks completely, or that
    contains_secret no longer detects. The message names the pattern and the context, never the value (L26)."""
    out = []
    for vid, kind, ctx, text, value in _cases():
        if (vid, ctx) in MAIN_MISSES:
            continue
        left = unmasked_pieces(value, redact_text(text))
        if left:
            out.append(f"{_what(kind, vid, ctx)}: redact_text leaves {left} of its {len(value) - PIECE + 1} "
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
    for vid, kind, ctx, text, value in _cases():
        if kind in GROUP_KINDS or (vid, ctx) in GUARD_EXEMPT or (vid, ctx) in GUARD_UNSEEN:
            continue
        spans = [(f.start, f.end) for f in find_high_confidence_secrets(text)]
        if not _overlaps(text, value, spans):
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
    """Each variant's secret lies inside one match of its own pattern: the whole body, every character of its class."""
    patterns = dict(redaction._NAMED_PATTERNS)
    wrong = [f"pattern {kind} no longer matches the whole of its variant {vid} (plain)"
             for vid, kind, item, value in VARIANTS
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
               "after_json_ansi_colour")
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
    (text, value), = [(item, value) for vid, _, item, value in VARIANTS if vid == "google_oauth"]
    redacted = redact_text(text)
    assert value not in redacted and redacted != text  # the old whole-value check stays green: that is the gap
    assert unmasked_pieces(value, redacted) >= 3
    regressions = redaction_regressions()
    for ctx in ("plain", "after_json_newline", "after_ansi_colour"):
        assert any(r.startswith(f"pattern google_oauth_token (variant google_oauth) in context {ctx}: redact_text "
                                "leaves ") for r in regressions), (ctx, regressions)
    assert all("google_oauth" in r for r in regressions), regressions  # no other pattern is blamed
    assert not any(value[i:i + PIECE] in r for r in regressions for i in range(len(value) - PIECE + 1))  # L26
