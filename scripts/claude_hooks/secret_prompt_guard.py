"""UserPromptSubmit hook: refuse a prompt that carries a high-confidence secret shape (AGENTS.md invariant 12,
LESSONS L1: "a secret seen in chat is burned").

    echo '{"prompt": "..."}' | python3 scripts/claude_hooks/secret_prompt_guard.py

Protocol (Claude Code hooks reference, https://code.claude.com/docs/en/hooks, sections "UserPromptSubmit" and
"What a blocked prompt leaves behind"):

* Input: JSON on stdin; the submitted text is the ``prompt`` field.
* Allow: print nothing and exit 0.
* Block: exit 0 and print ``{"decision": "block", "reason": "...", "hookSpecificOutput": {"hookEventName":
  "UserPromptSubmit", "suppressOriginalPrompt": true}}``. ``reason`` is shown to the user and is not added to the
  model context. ``suppressOriginalPrompt`` keeps the prompt text out of the block message, which Claude Code would
  otherwise end with ``Original prompt:`` plus the text and write to the session transcript. The docs say plainly that
  the text can still be in the transcript and the prompt history: a block keeps the secret from the model, it does
  not un-send it, so the reason tells the owner to rotate.
* Exit code 2 would also block, but this script never uses it: every internal error (bad JSON, missing prompt, an
  import failure, anything) exits 0 with no output, so a fault here can never obstruct the owner (fail-open). Do not
  add ``argparse`` (it exits 2 on a bad argument).

The detection reuses the canonical credential patterns of ``aeos.core.redaction`` (LESSONS L10/L17: one pattern
list). That module imports only the standard library, so the import is about 10 ms. Three deliberate differences,
each pinned by a test in ``tests/unit/test_secret_prompt_guard.py``:

* Only the token-shaped patterns are used (the ones without a capture group), as in ``contains_high_confidence_secret``;
  the generic ``password=...`` and ``Bearer ...`` rules are too noisy to block a prompt.
* The short Vault/OpenBao forms ``s.<24+>``, ``b.<24+>`` and ``r.<24+>`` are skipped: ``models.CustomerHistoryRecordX``
  in a question about code has that shape. ``hvs.``/``hvb.``/``hvr.`` tokens are still caught by their own pattern.
* Two kinds the canonical module does not cover yet (an ``ASIA`` temporary AWS key id, and a PEM private key header
  without its END line, which is what a truncated paste looks like) are in ``GAPS`` below; the header entry also
  replaces the canonical full-block pattern. A test fails when the
  canonical module starts to cover them, which is the signal to delete the entry here.

The reason names only the KIND of secret, never a part of the value. Stdlib only, Python 3.11+."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

sys.dont_write_bytecode = True  # a hook must not drop __pycache__ into the checkout

ROOT = Path(__file__).resolve().parents[2]

# Canonical patterns that stay out of the scan: the short Vault forms match ordinary prose or code, and the full PEM
# block pattern is superseded by the header-only entry in GAPS (the lazy "up to END" scan is quadratic on a prompt
# with many headers and no END, and a header alone is already enough to refuse).
SKIPPED_PATTERN_PREFIXES = (r"(?:hvs|s|b)\.", r"(?<![A-Za-z0-9.])r\.", "-----BEGIN")

# (kind, pattern) for shapes the canonical module does not define yet.
GAPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"(?<![A-Za-z0-9])ASIA[A-Z2-7]{16}(?![A-Za-z0-9])")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),  # BLOCK: ASCII-armored PGP
)

# Kind of a matched token, from its own leading characters (detection is done by the patterns, not by this table).
KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("sk-or-v1-", "OpenRouter API key"),
    ("sk-ant-", "Anthropic API key"),
    ("sk-", "OpenAI-style API key"),
    ("github_pat_", "GitHub token"),
    ("gh", "GitHub token"),
    ("AKIA", "AWS access key id"),
    ("ASIA", "AWS access key id"),
    ("AIza", "Google API key"),
    ("ya29.", "Google OAuth access token"),
    ("1//", "Google OAuth refresh token"),
    ("hv", "OpenBao/Vault token"),
    ("xox", "Slack token"),
    ("-----BEGIN", "private key"),
    ('"unseal', "OpenBao/Vault unseal keys"),
    ('"recovery', "OpenBao/Vault recovery keys"),
)
KIND_BY_LABEL: tuple[tuple[str, str], ...] = (
    ("unseal", "OpenBao/Vault unseal key"),
    ("recovery", "OpenBao/Vault recovery key"),
    ("initial root", "OpenBao/Vault root token"),
)
FALLBACK_KIND = "credential"


def classify(matched: str) -> str:
    for prefix, kind in KIND_BY_PREFIX:
        if matched.startswith(prefix):
            return kind
    lowered = matched.lower()
    for label, kind in KIND_BY_LABEL:
        if lowered.startswith(label):
            return kind
    return FALLBACK_KIND


def canonical_patterns() -> list[re.Pattern[str]]:
    """The high-confidence token patterns of aeos.core.redaction, without SKIPPED_PATTERN_PREFIXES."""
    sys.path.insert(0, str(ROOT / "backend"))
    from aeos.core.redaction import _PATTERNS

    return [p for p in _PATTERNS if not p.groups and not p.pattern.startswith(SKIPPED_PATTERN_PREFIXES)]


def detected_kinds(prompt: str) -> list[str]:
    """Distinct kinds of secret found in ``prompt``, in order of first appearance. Only the fixed kind labels of
    KIND_BY_PREFIX / KIND_BY_LABEL / GAPS leave this function, never matched text, so the block reason that names them
    carries no secret (CodeQL py/clear-text-logging-sensitive-data flagged the former name ``secret_kinds`` by its
    name alone)."""
    found: list[tuple[int, str]] = []
    for pattern in canonical_patterns():
        for m in pattern.finditer(prompt):
            found.append((m.start(), classify(m.group(0))))
    for kind, pattern in GAPS:
        for m in pattern.finditer(prompt):
            found.append((m.start(), kind))
    kinds: list[str] = []
    for _, kind in sorted(found):
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def block_decision(kinds: list[str]) -> dict[str, object]:
    names = ", ".join(kinds)
    reason = (
        f"Prompt DIBLOKIR: terdeteksi secret ({names}). Jangan tempel secret ke chat. "
        "Simpan dengan ./aeos secrets set. Jika sudah terkirim ke mana pun, rotasi sekarang "
        "(secret yang terlihat di chat dianggap bocor). "
        f"| Prompt BLOCKED: a secret was detected ({names}). Do not paste secrets into chat. "
        "Store it with ./aeos secrets set. If it was already sent anywhere, rotate it now "
        "(a secret seen in chat is burned)."
    )
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True},
    }


def decide(raw: str) -> dict[str, object] | None:
    """The block decision for the hook input ``raw``, or None to allow. Raises on nothing it can ignore."""
    data = json.loads(raw)
    prompt = data.get("prompt") if isinstance(data, dict) else None
    if not isinstance(prompt, str) or not prompt:
        return None
    kinds = detected_kinds(prompt)
    return block_decision(kinds) if kinds else None


def main(read: Callable[[], bytes] | None = None) -> int:
    """Always 0: this hook never obstructs the owner by failing."""
    try:
        raw = (read() if read else sys.stdin.buffer.read()).decode("utf-8", "replace")
        decision = decide(raw)
        if decision is not None:
            sys.stdout.write(json.dumps(decision))
            sys.stdout.flush()
    except BaseException:  # noqa: S110 - fail-open on anything, including a broken pipe
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
