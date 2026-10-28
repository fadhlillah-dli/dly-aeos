"""Secret redaction never gets weaker (LESSONS L25, ADR-0127; secret class, so the tool is due at strike 1).

Incident (2026-10-28): a change to the secret patterns made for the broker's outgoing-content guard added a
look-behind to the short Vault token pattern, and log redaction stopped masking a token glued to a letter, such as
one after a JSON-escaped newline (``"$ cat ~/.vault-token\\nhvs...."`` in a worker log). A blind differential review
caught it before merge.

This test is that differential, kept: a committed corpus of realistic secret shapes, one or more variants for every
pattern of ``aeos.core.redaction`` as origin/main defined them when the incident was found, placed in many contexts
(plain, after a letter or digit, after a JSON escape, after a URL-encoded separator, inside a data URI, inside a JSON
string, a multi-line PEM block). It asserts that

* ``redact_text`` masks and ``contains_secret`` detects every (variant, context) that origin/main caught: the only
  pairs it may miss are listed in ``MAIN_MISSES`` (derived by running origin/main's patterns over this corpus);
* the outgoing guard (``find_high_confidence_secrets`` and the broker's ``sensitive_content_findings``) finds every
  token-shaped item in every context except the documented base64 exception (a Google key or refresh token glued to
  a letter or digit reads as base64 data), including after escapes origin/main's redaction misses;
* near misses (code identifiers, base64 image data, ``sha512-`` lockfile integrity hashes, also JSON-encoded) are not
  flagged by the guard.

A pattern change that drops a case is red with a message naming the pattern (detector kind) and the context. A new
pattern needs a corpus variant here (the coverage test names it). Every sample is assembled from parts at run time,
so the repository secret scan does not flag this file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
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

_B64_KEY = "q7Lm2Np4Rs6Tv8Xb1Cd3Fg5Hj9Kw0Ya2Ue4Zx9Qw2Er4T" + "/y+="
_B64_KEY2 = "Zx9Qw2Er4Ty6Ui8Op0As3Df5Gh7Jk9Lz1Xc3Vb5Nm7Qw" + "+/a="
_PEM_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
_TOKEN_BODY = "Zq7Lm2Np4Rs6Tv8Xb1Cd3Fg5Hj9Kw0Ya2Ue4"


def _pem(kind: str) -> tuple[str, str]:
    item = ("-----BEGIN " + kind + "PRIVATE " + "KEY-----\n" + _PEM_LINE + "\n" + _PEM_LINE[::-1] + "\n-----END "
            + kind + "PRIVATE " + "KEY-----")
    return item, _PEM_LINE


def _variants() -> list[tuple[str, str, str, str]]:
    """(variant id, detector kind, item placed in a context, the secret value that must not survive redaction)."""
    out: list[tuple[str, str, str, str]] = []

    def add(vid: str, kind: str, item: str, value: str | None = None) -> None:
        out.append((vid, kind, item, item if value is None else value))

    add("openrouter", "openrouter_key", "sk-" + "or-v1-" + "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e")
    add("anthropic", "anthropic_key", "sk-" + "ant-" + "api03-" + _TOKEN_BODY + "_Rt-9Yp")
    add("openai_project", "openai_style_key", "sk-" + "proj-" + "Q2w4E6r8T0y1U3i5O7p9A2s4D6f8G0h1J3k5")
    for c in "pousr":
        add(f"github_gh{c}", "github_token", "gh" + c + "_" + _TOKEN_BODY)
    add("github_pat", "github_pat", "github" + "_pat_" + "11ABCDEFG0" + _TOKEN_BODY)
    add("aws_key_id", "aws_access_key_id", "AK" + "IA" + "Q7W3E9R1T5Y2U8I4")
    add("google_api_key", "google_api_key", "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
    add("google_oauth", "google_oauth_token", "ya" + "29." + "a0AfH6SMBx7Kq2Lm9Np4Rs6Tv8Xb1Cd3Fg5Hj9Kw0")
    add("google_refresh", "google_refresh_token", "1/" + "/0g" + "Xk2Lm9Qp_7Rt-4Vw8Yz1Bc3De5Fg6Hj8Kl0Nm2Pq4")
    for c in "sbr":
        add(f"vault_hv{c}", "vault_token", "hv" + c + "." + "CAESIJlWh1Ad0nFpQ8ZqXyTb7cWo3kLmN4rS5tUvWxYz")
    for c in "sb":
        add(f"vault_short_{c}", "vault_token_short", c + "." + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4")
    add("openbao_recovery", "openbao_recovery_token", "r" + "." + "Zx9Qw2Er4Ty6Ui8Op0As3Df5Gh7")
    for label in ("Unseal Key 1", "Recovery Key 3", "unseal key share 2", "Unseal Key"):
        add("unseal_" + label.lower().replace(" ", "_"), "vault_unseal_key", label + ": " + _B64_KEY, _B64_KEY)
    add("root_token", "vault_root_token", "Initial Root Token: " + "7b3e9a1c-5d2f-4e8a-b6c0-1f9d3e7a5b2c",
        "7b3e9a1c-5d2f-4e8a-b6c0-1f9d3e7a5b2c")
    for which in ("unseal", "recovery"):
        for enc in ("b64", "hex"):
            add(f"init_json_{which}_{enc}", "vault_init_json",
                json.dumps({f"{which}_keys_{enc}": [_B64_KEY, _B64_KEY2]}), _B64_KEY)
    for c in "baprs":
        add(f"slack_xox{c}", "slack_token", "xox" + c + "-" + "1234567890-" + "Zq7Lm2Np4Rs6")
    for label in ("", "RSA ", "EC ", "OPENSSH "):
        item, value = _pem(label)
        add("pem_" + (label.strip().lower() or "pkcs8"), "private_key_pem", item, value)
    add("bearer", "bearer_value", "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.Zq7Lm2Np4Rs6Tv8Xb1",
        "eyJhbGciOiJIUzI1NiJ9.Zq7Lm2Np4Rs6Tv8Xb1")
    for label in ("password=", "passwd: ", "secret=", "token: ", "api_key=", "api-key: ", "apikey="):
        add("assign_" + re.sub(r"\W", "", label), "credential_assignment", label + "Hunter2CorrectHorse9",
            "Hunter2CorrectHorse9")
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
    "data_uri": lambda t: "data:text/plain;charset=utf-8," + t,
}
GLUED = frozenset({"after_letter", "after_digit"})

# (variant id, context) pairs origin/main's redact_text did NOT mask (derived on 2026-10-28 by running origin/main's
# patterns over this corpus). Everything else must stay masked. Shrinking this set is fine; adding to it weakens
# redaction and needs an owner decision recorded in an ADR.
# Contexts in which the token directly follows a letter or digit: glued, or after an escape that ends in one
# (``\n``, ``\t``, ``\u00e9``, ``%3D``, ``%26``, ``%3F``).
_LETTER_OR_DIGIT_BEFORE = ("after_letter", "after_digit", "after_json_newline", "after_json_tab", "after_json_unicode",
                           "double_json_newline", "after_url_eq", "after_url_amp", "after_url_question")
# Contexts in which JSON escaping turned the quotes of a labelled JSON shape into ``\"``.
_JSON_ENCODED = ("json_string", "after_json_newline", "after_json_tab", "after_json_quote", "after_json_unicode",
                 "double_json_newline")
MAIN_MISSES = frozenset(
    # a look-behind (``sk-``, ``hvr.``, ``r.``) or a word boundary (the unseal-key label) needs a non-alphanumeric
    # character before the shape
    [(vid, ctx) for vid in ("openrouter", "anthropic", "openai_project", "vault_hvr", "openbao_recovery",
                            "unseal_unseal_key_1", "unseal_recovery_key_3", "unseal_unseal_key_share_2",
                            "unseal_unseal_key")
     for ctx in _LETTER_OR_DIGIT_BEFORE]
    # the `operator init` JSON pattern needs literal quotes
    + [(f"init_json_{which}_{enc}", ctx) for which in ("unseal", "recovery") for enc in ("b64", "hex")
       for ctx in _JSON_ENCODED])
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


def redaction_regressions() -> list[str]:
    """Every (variant, context) origin/main masked that redact_text or contains_secret no longer does."""
    out = []
    for vid, kind, ctx, text, value in _cases():
        if (vid, ctx) in MAIN_MISSES:
            continue
        if value in redact_text(text):
            out.append(f"{_what(kind, vid, ctx)}: redact_text no longer masks it (origin/main did)")
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


def test_each_variant_is_still_matched_by_its_own_pattern():
    patterns = dict(redaction._NAMED_PATTERNS)
    wrong = [f"pattern {kind} no longer matches its variant {vid} (plain)" for vid, kind, item, _ in VARIANTS
             if not patterns[kind].search(item)]
    assert not wrong, "\n".join(wrong)


def test_redaction_masks_everything_origin_main_masked():
    regressions = redaction_regressions()
    assert not regressions, ("secret redaction got weaker (LESSONS L25):\n" + "\n".join(regressions))


def test_the_outgoing_guard_finds_every_token_in_every_context():
    misses = guard_misses()
    assert not misses, ("the outgoing guard misses a credential (ADR-0127):\n" + "\n".join(misses))


def test_the_broker_refuses_every_token_after_a_json_escape_in_a_tool_result():
    from aeos.broker.model_proxy import sensitive_content_findings

    escaped = ("after_json_newline", "after_json_tab", "after_json_quote", "after_json_unicode", "double_json_newline")
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


NEAR_MISSES = {
    "js_members": "this" + ".handleSubmitButtonClickedEventNow(); lib" + ".someLongFunctionNameForTheParser();",
    "go_receivers": ("return s" + ".registerServiceWithHealthCheck(ctx)\n b" + ".ReportAllocsAndResetTimerNow()\n"
                     "rows, err := r" + ".findAllByOrganizationIdentifier(id)\n"),
    "python_model_member": "see model" + "s" + ".CustomerSubscriptionHistoryRecord",
    "floor_division": "pages = total1/" + "/itemsPerPageForThisListing",
    "image_data_uri": _image_data_uri(),
    "lockfile_sha512": _lockfile(5_000),
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
    assert "pattern vault_token_short (variant vault_short_s) in context after_letter: redact_text no longer masks " \
           "it (origin/main did)" in regressions
    assert any("vault_token_short" in r and "after_json_newline" in r for r in regressions), regressions


def test_bite_a_dropped_pattern_is_red_and_the_unchanged_patterns_are_green(monkeypatch):
    assert redaction_regressions() == [] and guard_misses() == []  # the near miss: today's patterns
    named = [(k, p) for k, p in redaction._NAMED_PATTERNS if k != "slack_token"]
    monkeypatch.setattr(redaction, "_NAMED_PATTERNS", named)
    monkeypatch.setattr(redaction, "_PATTERNS", [p for _, p in named])
    assert "pattern slack_token (variant slack_xoxb) in context plain: redact_text no longer masks it (origin/main " \
           "did)" in redaction_regressions()
    assert "pattern slack_token (variant slack_xoxb) in context data_uri: the outgoing guard does not find it" \
           in guard_misses()
