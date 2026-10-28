"""Grammar fuzz of the escape boundary (ADR-0127 decision 7, LESSONS L25): the loop-until-dry tool.

Three reviews of 2026-10-28 each found one more escape form in front of a token that the outgoing guard did not treat
as a boundary (a colour code written as text, the OSC-8 terminator, a bare ESC + ``[`` and the textual BEL forms). Instead
of one more context per review, this test generates the contexts: ``N_PREFIXES`` prefixes from a small grammar of
escape atoms (newline, tab, quote, backslash, BEL, NUL, other control bytes, a non-ASCII letter, CSI sequences with and
without parameters, a bare CSI introducer, a CSI cut off after its parameters, a CSI with a quote, apostrophe or slash
intermediate, OSC sequences ended by BEL or by ESC + backslash, a bare ESC and other ESC sequences, ``ESC % G``),
chained one to four, each atom written raw, JSON-escaped, JSON-escaped twice, as C/Python/shell text, or
URL-encoded (once or twice, over any of those). Every corpus secret of test_redaction_never_weaker is placed after
every prefix and before a generated suffix, and the test asserts that

* the outgoing guard (``find_high_confidence_secrets``) finds every token-shaped secret, and
* ``redact_text`` leaves no ``PIECE``-character piece of any part of the secret, except where origin/main itself misses
  it: a look-behind shape (``sk-``, ``hv[sbr].``, ``r.``, the unseal-key label) right after an ASCII letter or digit
  (the derivation of MAIN_MISSES; the redaction patterns are byte-identical to origin/main's).

The seeds are fixed, so a failure is reproducible; a message names the detector kind, the corpus variant and the
grammar of the prefix (atoms and encodings), never a value (LESSONS L26). A future escape form that is not a boundary
shows up here as a miss. Cases are checked in batches (one text per variant, segments separated by a line no pattern
can cross), one test per corpus variant so pytest-xdist spreads them.
"""

from __future__ import annotations

import json
import random
import re
import urllib.parse
from bisect import bisect_right
from collections.abc import Callable
from itertools import accumulate

import pytest

from aeos.core import redaction
from aeos.core.redaction import find_high_confidence_secrets, redact_text
from tests.unit.test_redaction_never_weaker import GROUP_KINDS, PIECE, VARIANTS, greedy_view_only

N_PREFIXES = 5000
SEED = 20261028
SEPARATOR = "\n|||\n"  # no pattern crosses a newline into a run of bars, and no prefix or suffix holds one
LOOK_BEHIND_KINDS = frozenset({"openrouter_key", "anthropic_key", "openai_style_key", "vault_token",
                               "openbao_recovery_token", "vault_unseal_key"})
_ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")

ESC, BEL = "\x1b", "\x07"


def _atoms(rng: random.Random) -> dict[str, Callable[[], str]]:
    """Logical escape atoms (before encoding). Each returns the decoded text of one escape."""
    params = ["31", "0", "1;32", "38;5;196", "48;2;10;20;30", "2;4", "1", "7"]
    finals = "mKHJABCDGfsu"
    return {
        "newline": lambda: "\n", "tab": lambda: "\t", "cr": lambda: "\r", "quote": lambda: '"',
        "apostrophe": lambda: "'", "backslash": lambda: "\\", "slash": lambda: "/", "backspace": lambda: "\b",
        "formfeed": lambda: "\f", "vtab": lambda: "\v", "nul": lambda: "\x00", "bel": lambda: BEL,
        "e_acute": lambda: "é",
        "c0": lambda: chr(rng.choice([c for c in range(1, 32) if c not in (9, 10, 13, 27)])),
        "csi_params": lambda: ESC + "[" + rng.choice(params) + rng.choice(finals),
        "csi_no_params": lambda: ESC + "[" + rng.choice(finals),
        "csi_intermediate": lambda: ESC + "[" + rng.choice(["2", "", "6"]) + " q",
        "csi_tilde": lambda: ESC + "[3~",
        "csi_introducer": lambda: ESC + "[",
        # an intermediate that JSON and source text escape: DECSCA ``CSI 1 " q``, DECIC ``CSI 2 ' }``
        "csi_escaped_intermediate": lambda: ESC + "[" + rng.choice(["0", "1", "", "61;1", "2"])
        + rng.choice(['"', "'", "/"]) + rng.choice("pq}~"),
        # cut off after its parameters (no final byte), the last one not a digit: ``;``, ``:``, ``?`` (URL-encoded,
        # their hex digits must not read as a token start)
        "csi_open_params": lambda: ESC + "[" + rng.choice(["1;", "31;", "38:5:", "2;4;", "?", "1;2:"]),
        "osc8_st": lambda: ESC + "]8;;https://h.example/x" + ESC + "\\",
        "osc_title_bel": lambda: ESC + "]0;build log" + BEL,
        "osc_introducer": lambda: ESC + "]",
        "string_terminator": lambda: ESC + "\\",
        "esc_charset": lambda: ESC + "(B",
        "esc_docs": lambda: ESC + rng.choice(["%G", "%@", "%/G"]),  # ISO 2022 coding system: a ``%`` intermediate
        "esc_single": lambda: ESC + rng.choice("78=>"),
        "esc_bare": lambda: ESC,
    }


# A printable atom written raw is plain text, not an escape: ``/`` is a base64 character (a Google key after it reads
# as glued, the documented residual) and a raw non-ASCII letter is a word character (``\b`` of the unseal label). They
# appear only in an escaped form.
_NOT_RAW = frozenset({"slash", "e_acute"})


def _textual(rng: random.Random, s: str) -> str:
    """*s* written as C, Python or shell source text (each character in one of the forms those languages accept)."""
    out = []
    for ch in s:
        o = ord(ch)
        forms = {ESC: ["\\e", "\\E", "\\x1b", "\\x1B", "\\033", "\\33", "\\u001b", "\\U0000001b", "\\u{1b}", "^["],
                 BEL: ["\\a", "\\x07", "\\007", "\\u0007", "\\u{7}"], "\x00": ["\\0", "\\x00", "\\000"],
                 "\n": ["\\n"], "\t": ["\\t"], "\r": ["\\r"], "\b": ["\\b"], "\f": ["\\f"], "\v": ["\\v"],
                 '"': ['\\"'], "'": ["\\'"], "\\": ["\\\\"], "/": ["\\/"],
                 "é": ["\\xe9", "\\xc3\\xa9", "\\u00e9", "\\351"]}.get(ch)
        if forms is None and o < 32:
            forms = [f"\\x{o:02x}", f"\\{o:03o}", f"\\u{o:04x}"]
        out.append(rng.choice(forms) if forms else ch)
    return "".join(out)


def _json(s: str) -> str:
    return json.dumps(s).replace("/", "\\/")[1:-1]


ENCODINGS: dict[str, Callable[[random.Random, str], str]] = {
    "raw": lambda rng, s: s,
    "json": lambda rng, s: _json(s),
    "json_twice": lambda rng, s: _json(_json(s)),
    "textual": _textual,
    "url": lambda rng, s: urllib.parse.quote(s, safe=""),
    "url_json": lambda rng, s: urllib.parse.quote(_json(s), safe=""),
    "url_textual": lambda rng, s: urllib.parse.quote(_textual(rng, s), safe=""),
    "url_twice": lambda rng, s: urllib.parse.quote(urllib.parse.quote(s, safe=""), safe=""),
}
LEADS = ("", "log ", "x", "7", "value=", "Z")


def _chain(rng: random.Random, atoms: dict[str, Callable[[], str]], low: int, high: int) -> tuple[str, str]:
    """One to four atoms, each in an encoding (or the whole chain in one): (text, grammar description)."""
    names = [rng.choice(sorted(atoms)) for _ in range(rng.randint(low, high))]
    whole = rng.random() < 0.3
    encoding = rng.choice(sorted(ENCODINGS))
    parts, grammar = [], []
    for name in names:
        enc = encoding if whole else rng.choice(sorted(ENCODINGS))
        if enc == "raw" and name in _NOT_RAW:
            enc = "json"
        parts.append(ENCODINGS[enc](rng, atoms[name]()))
        grammar.append(f"{name}/{enc}")
    return "".join(parts), "+".join(grammar)


def prefixes(n: int = N_PREFIXES, seed: int = SEED) -> list[tuple[str, str, str]]:
    """*n* (head: lead + prefix, suffix, grammar) triples, deterministic for *seed*."""
    rng = random.Random(seed)
    atoms = _atoms(rng)
    out = []
    for _ in range(n):
        prefix, grammar = _chain(rng, atoms, 1, 4)
        suffix, suffix_grammar = _chain(rng, atoms, 1, 2)
        lead = rng.choice(LEADS)
        out.append((lead + prefix, suffix + rng.choice(("", " end", " and more text")),
                    f"lead {lead!r} prefix {grammar} suffix {suffix_grammar}"))
    return out


PREFIXES = prefixes()


def _ngrams(parts: tuple[str, ...]) -> frozenset[str]:
    return frozenset(part[i:i + PIECE] for part in parts for i in range(len(part) - PIECE + 1))


def fuzz_misses(vids: frozenset[str], cases: list[tuple[str, str, str]] = PREFIXES) -> list[str]:
    """Every (variant, prefix) of the given corpus variants that the guard does not find, or that redact_text leaves a
    piece of where origin/main masked it whole. Names the kind, the variant and the grammar, never a value (L26)."""
    out: list[str] = []
    for vid, kind, item, parts in VARIANTS:
        if vid not in vids:
            continue
        segments = [head + item + suffix for head, suffix, _ in cases]
        text = SEPARATOR.join(segments)
        first_part, pos = [], 0  # where the first part of the secret starts in *text*, per segment
        for seg, (head, _, _) in zip(segments, cases, strict=True):
            first_part.append(pos + len(head) + item.find(parts[0]))
            pos += len(seg) + len(SEPARATOR)
        if kind not in GROUP_KINDS:
            spans = sorted((f.start, f.end) for f in find_high_confidence_secrets(text))
            starts = [s for s, _ in spans]
            reach = list(accumulate((e for _, e in spans), max))
            for at, (_, _, grammar) in zip(first_part, cases, strict=True):
                i = bisect_right(starts, at + len(parts[0]) - 1)  # findings that start before the part ends
                if i == 0 or reach[i - 1] <= at:
                    out.append(f"pattern {kind} (variant {vid}) after {grammar}: the outgoing guard does not find it")
        grams = _ngrams(parts)
        redacted = redact_text(text).split(SEPARATOR)
        assert len(redacted) == len(cases), "a pattern crossed the segment separator"
        for (head, _, grammar), seg in zip(cases, redacted, strict=True):
            if kind in LOOK_BEHIND_KINDS and head and head[-1] in _ALNUM:
                continue  # origin/main's miss (MAIN_MISSES): the shape right after an ASCII letter or digit
            left = sum(seg[i:i + PIECE] in grams for i in range(len(seg) - PIECE + 1))
            if left:
                out.append(f"pattern {kind} (variant {vid}) after {grammar}: redact_text leaves {left} "
                           f"{PIECE}-character pieces unmasked (origin/main masked all)")
    return out


VARIANT_IDS = [vid for vid, _, _, _ in VARIANTS]


def test_the_grammar_generates_enough_distinct_prefixes():
    assert len(PREFIXES) >= 5000
    assert len({p for p, _, _ in PREFIXES}) >= 4500
    grammar = " ".join(g for _, _, g in PREFIXES)
    for atom in _atoms(random.Random(0)):
        for enc in ENCODINGS:
            if not (enc == "raw" and atom in _NOT_RAW):
                assert f"{atom}/{enc}" in grammar, (atom, enc)


@pytest.mark.parametrize("vid", VARIANT_IDS)
def test_every_corpus_secret_after_every_generated_escape_prefix(vid):
    misses = fuzz_misses(frozenset({vid}))
    assert not misses, f"{len(misses)} misses (first 20):\n" + "\n".join(misses[:20])


def test_bite_the_fuzz_is_red_without_the_cut_view_and_names_the_grammar(monkeypatch):
    """The violating fixture: the boundary read greedily only. The fuzz is red, naming the kind, the variant and the
    prefix grammar (a bare CSI introducer before the token), never the value. The near miss is the test above."""
    monkeypatch.setattr(redaction, "_boundary_views", greedy_view_only)
    misses = fuzz_misses(frozenset({"anthropic", "anthropic_long"}), PREFIXES[:400])
    assert any("csi_introducer/" in m.split(" suffix ")[0].rsplit("+", 1)[-1] and "outgoing guard does not find it" in m
               for m in misses), misses[:5]
    secrets = [item for _, kind, item, _ in VARIANTS if kind == "anthropic_key"]
    assert not any(item[4:4 + PIECE] in m for m in misses for item in secrets)  # L26


def _last_atom(grammar: str) -> str:
    """The atom (name/encoding) of a prefix grammar that sits directly before the secret."""
    return grammar.split(" prefix ", 1)[1].split(" suffix ", 1)[0].rsplit("+", 1)[-1]


# The escape boundary of e143cd0: an intermediate byte only raw or URL-encoded and never ``%``, and a cut view that
# keeps an escape tail from its first token-start character, also a hex digit inside an encoded byte.
_INTERMEDIATE_E143CD0 = redaction._raw_or_encoded(r"[ -$&-/]", "2[0-46-9A-Fa-f]")


def _first_token_start_e143cd0(text: str, tail: int, end: int) -> int:
    first = redaction._TOKEN_START_RE.search(text, tail, end)
    return first.start() if first else end


@pytest.mark.parametrize("old, atoms", [
    ("intermediate", ("esc_docs/", "csi_escaped_intermediate/json")),
    ("tail", ("csi_open_params/url",)),
])
def test_bite_the_e143cd0_boundary_misses_a_google_token_after_the_new_atoms(monkeypatch, old, atoms):
    """Review of e143cd0 (2026-10-28): ``ESC % G`` and a CSI whose quote or slash intermediate is JSON-escaped left
    their final letter glued to a Google key or refresh token, and a URL-encoded CSI cut off after its parameters
    started the cut view at the ``A`` of ``%3A``, so the encoded bytes stayed glued to the token: red, naming the kind,
    the variant and the atom before the token. Today's boundary is the near miss (the fuzz above is green)."""
    if old == "intermediate":
        boundary = re.compile(redaction._ESCAPE_BOUNDARY.pattern.replace(redaction._INTERMEDIATE,
                                                                          _INTERMEDIATE_E143CD0))
        assert boundary.pattern != redaction._ESCAPE_BOUNDARY.pattern
        monkeypatch.setattr(redaction, "_ESCAPE_BOUNDARY", boundary)
    else:
        monkeypatch.setattr(redaction, "_tail_token_start", _first_token_start_e143cd0)
    cases = [c for c in PREFIXES if _last_atom(c[2]).startswith(atoms)]
    misses = fuzz_misses(frozenset({"google_api_key_1", "google_refresh"}), cases)
    for atom in atoms:
        assert any(_last_atom(m.split(" after ", 1)[1]).startswith(atom) and "outgoing guard does not find it" in m
                   for m in misses), (atom, misses[:5])
    secrets = [item for _, kind, item, _ in VARIANTS if kind in ("google_api_key", "google_refresh_token")]
    assert not any(item[4:4 + PIECE] in m for m in misses for item in secrets)  # L26
