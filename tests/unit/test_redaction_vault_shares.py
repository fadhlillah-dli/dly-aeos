"""Vault/OpenBao ``operator init`` output is redacted and detected (incident 2026-10-26: an unseal key was
pasted into a chat twice). Only labelled forms match, so a bare base64 string never does.

Every sample is assembled from parts at run time so the repository secret scan does not flag this file."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import subprocess
import sys

import pytest

from aeos.core import redaction
from aeos.core.paths import repo_root
from aeos.core.redaction import (
    REDACTED,
    UNBOUNDED_TOKEN_PATTERNS,
    contains_high_confidence_secret,
    contains_secret,
    redact,
    redact_text,
)

SHARE = base64.b64encode(bytes(range(7, 40))).decode("ascii")  # 44 base64 characters, like a real share
OTHER_SHARE = base64.b64encode(bytes(range(90, 123))).decode("ascii")
SERVICE_TOKEN = "hv" + "s." + "CAESI" + "j" * 20 + "_-" + "k" * 10
ROOT_TOKEN = "s" + "." + "a1B2c3D4e5F6g7H8i9J0k1L2"
RECOVERY_TOKEN = "r" + "." + "Q7w3E9r1T5y2U8i4O6p0A3s5"


def _label(n: int | None = 1) -> str:
    return "Unseal " + "Key" + ("" if n is None else f" {n}") + ": "


def _vault_patterns() -> list[re.Pattern[str]]:
    return [
        p
        for p in redaction._PATTERNS
        if not p.groups and any(k in p.pattern for k in ("unseal", "initial root", "hv[sbr]", r")r\."))
    ]


def _leaks(text: str, secret: str) -> bool:
    return secret in redact_text(text)


# ------------------------------------------------------------------------- bite: violating is red
@pytest.mark.parametrize(
    "text, secret",
    [
        (_label(1) + SHARE, SHARE),
        (_label(5) + OTHER_SHARE + "\n", OTHER_SHARE),
        (_label(None) + SHARE, SHARE),
        ("unseal key 2:   " + SHARE, SHARE),
        ("UNSEAL KEY 3: " + SHARE, SHARE),
        ("Unseal " + "Key Share 1: " + SHARE, SHARE),
        ("Recovery " + "Key 1: " + SHARE, SHARE),
        ("log: " + _label(2) + SHARE + " (end)", SHARE),
        ("Initial Root " + "Token: " + ROOT_TOKEN, ROOT_TOKEN),
        ("Initial Root " + "Token: " + SERVICE_TOKEN, SERVICE_TOKEN),
        ("initial root token:" + ROOT_TOKEN, ROOT_TOKEN),
        ("token " + SERVICE_TOKEN, SERVICE_TOKEN),
        ("hv" + "b." + "x" * 30, "x" * 30),
        ("hv" + "r." + "y" * 30, "y" * 30),
        ("Recovery " + "token: " + RECOVERY_TOKEN, RECOVERY_TOKEN),
        ("token " + RECOVERY_TOKEN + ".", RECOVERY_TOKEN),
        ("(" + RECOVERY_TOKEN + ")", RECOVERY_TOKEN),
        ('{"unseal_keys_' + 'b64": ["' + SHARE + '", "' + OTHER_SHARE + '"]}', OTHER_SHARE),
        ('"unseal_keys_' + 'hex" : [ "' + "ab" * 33 + '" ]', "ab" * 33),
    ],
)
def test_labelled_vault_init_output_is_detected_and_redacted(text, secret):
    assert contains_secret(text)
    assert contains_high_confidence_secret(text)
    assert not _leaks(text, secret)
    assert REDACTED in redact_text(text)
    assert secret not in str(redact({"output": text}))


def test_all_shares_of_a_multi_line_init_output_are_redacted():
    text = "\n".join(_label(i) + SHARE[:-4] + f"{i:04d}" for i in range(1, 6)) + "\n\nInitial Root " + "Token: " + ROOT_TOKEN
    out = redact_text(text)
    assert out.count(REDACTED) == 6
    assert SHARE[:-4] not in out and ROOT_TOKEN not in out


def test_the_binary_scan_patterns_also_see_labelled_forms():
    blob = b"\x00\x01" + (_label(1) + SHARE).encode("ascii") + b"\xff"
    assert any(p.search(blob.decode("latin-1")) for p in UNBOUNDED_TOKEN_PATTERNS)


# ----------------------------------------------------------------- bite: legitimate near-miss is green
@pytest.mark.parametrize(
    "text",
    [
        SHARE,  # base64 without the label
        "sha256-" + SHARE + "=",  # a lockfile / subresource integrity hash
        "integrity: " + SHARE,
        "Store each unseal key offline; an unseal key is never pasted into chat.",
        "Unseal " + "Key: store it offline",
        "Unseal " + "Key 1: <paste it here>",
        "Unseal " + "Key 1: stored",
        "Unseal " + "Key 1: " + "x" * 10,  # too short to be a share
        "Unseal " + "Key 1: " + SHARE[:31],
        "Unseal " + "Key 1: " + SHARE + "-continues-with-hyphens",
        "the unseal key quorum: " + "three of five",
        "Initial Root " + "Token: revoked",
        "Initial Root " + "Token: <token>",
        "Initial Root " + "Token: " + "t" * 10,
        "the initial root token is revoked after setup",
        "Unseal Keys: 5, threshold 3",
        '"unseal_keys_' + 'b64": []',
        '"unseal_keys_' + 'b64": ["<share>"]',
        "unseal_keys_b64 is a field name",
        "hvs. alone, hv" + "s.short",
        "r" + ".Q7w3E9r1T5y2U8i4O6p0A3",  # 22 characters: too short
        "user" + ".Q7w3E9r1T5y2U8i4O6p0A3s5",  # r. inside a word or dotted name
        "foo." + RECOVERY_TOKEN,  # preceded by a dot: an attribute chain, not a token
        "Recovery token: " + "r" + ".short",
        "unsealed" + ": " + SHARE,
    ],
)
def test_unlabelled_or_placeholder_forms_are_not_detected(text):
    assert not contains_high_confidence_secret(text), text
    assert not [p.pattern for p in _vault_patterns() if p.search(text)], text


# ------------------------------------------------------------- repository wide false-positive rate
def test_new_patterns_have_no_false_positive_in_tracked_files():
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = repo_root()
    proc = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    new = _vault_patterns()
    assert len(new) >= 5
    assert any(r")r\." in p.pattern for p in new)
    hits: list[str] = []
    for rel in proc.stdout.splitlines():
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}: {m.group(0)[:20]}..." for p in new for m in p.finditer(text)]
    assert not hits, "\n".join(hits)


# --------------------------------------- whole-file scan in scripts/secret_scan.py (pretty-printed init)
def _secret_scan():
    path = repo_root() / "scripts" / "secret_scan.py"
    spec = importlib.util.spec_from_file_location("aeos_script_secret_scan_vault", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _init_json(shares: list[str], indent: int | None) -> str:
    return json.dumps({"unseal_keys_" + "b64": shares, "unseal_threshold": 2}, indent=indent)


def test_secret_scan_flags_a_pretty_printed_init_json_but_not_per_line():
    scan = _secret_scan()
    text = _init_json([SHARE, OTHER_SHARE], indent=2)
    assert text.count("\n") > 4
    assert not [ln for ln in text.splitlines() if any(p.search(ln) for p in scan.HIGH_CONFIDENCE)]
    found = scan.multiline_findings("docs/leak.json", text)
    assert len(found) == 1 and found[0].startswith("docs/leak.json:") and "multi-line" in found[0]


def test_secret_scan_reports_the_line_of_the_multiline_match():
    scan = _secret_scan()
    text = "line one\nline two\n" + _init_json([SHARE], indent=4)
    assert scan.multiline_findings("a.json", text)[0].startswith("a.json:4:")


def test_secret_scan_whole_file_pass_ignores_placeholders_and_single_line_matches():
    scan = _secret_scan()
    assert scan.multiline_findings("docs/ok.json", _init_json(["<share>", "<share>"], indent=2)) == []
    assert scan.multiline_findings("docs/ok.json", _init_json([], indent=2)) == []
    assert scan.multiline_findings("docs/ok.md", "no secret here\n" + SHARE + "\n") == []
    # a compact single-line init JSON is already reported by the per-line loop, not twice
    assert scan.multiline_findings("a.json", _init_json([SHARE], indent=None)) == []
