"""Official binary authenticity report (master §12 "use the unmodified official Claude Code binary")."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aeos.claude_bridge import binary as binmod


def _runner(responses):
    calls = []

    def run(argv, timeout=30):
        calls.append(argv)
        for key, value in responses.items():
            if key in " ".join(argv):
                return value
        return (1, "", "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode path")
@pytest.mark.parametrize("os_status,expected", [("Valid", "VALID"), ("NotSigned", "UNSIGNED"),
                                                ("HashMismatch", "INVALID"), ("Weird", "UNKNOWN")])
def test_windows_authenticode_result_is_reported_not_assumed(os_status, expected):
    run = _runner({"Get-AuthenticodeSignature": (0, json.dumps({"Status": os_status, "Signer": "CN=Example"}), "")})
    sig = binmod.publisher_signature(Path("C:/x/claude.exe"), run=run)
    assert sig["status"] == expected and sig["method"] == "Authenticode"
    assert "UNVERIFIED" in sig["publisher_pin"]  # no pinned publisher identity: authenticity is not claimed


@pytest.mark.skipif(sys.platform != "darwin", reason="codesign path")
def test_macos_unsigned_binary_is_reported():
    run = _runner({"--verify": (1, "", "code object is not signed at all"), "-dv": (1, "", "not signed")})
    assert binmod.publisher_signature(Path("/x/claude"), run=run)["status"] == "UNSIGNED"


def test_missing_binary_or_failing_check_is_unknown_never_valid():
    assert binmod.publisher_signature(None)["status"] == "UNKNOWN"

    def boom(argv, timeout=30):
        raise OSError("no shell")

    assert binmod.publisher_signature(Path("claude"), run=boom)["status"] == "UNKNOWN"


ANTHROPIC = "CN=Anthropic, PBC, O=Anthropic, PBC, L=San Francisco, S=California, C=US"


@pytest.mark.parametrize(("sig", "pinned", "gate"), [
    ({"status": "INVALID", "signer": ANTHROPIC}, None, "BLOCKED"),       # HashMismatch / NotTrusted
    ({"status": "INVALID", "signer": ANTHROPIC}, "Anthropic", "BLOCKED"),
    ({"status": "VALID", "signer": "CN=Someone Else"}, "Anthropic", "BLOCKED"),  # signer drift from the pin
    ({"status": "UNSIGNED", "signer": None}, None, "DEGRADED"),
    ({"status": "VALID", "signer": ANTHROPIC}, "Anthropic, PBC", "OK"),
    ({"status": "VALID", "signer": ANTHROPIC}, None, "UNVERIFIED"),       # no pin: never shown as authentic
    ({"status": "UNKNOWN"}, None, "UNVERIFIED"),
    (None, "Anthropic", "UNVERIFIED"),
])
def test_signature_gate_blocks_invalid_or_foreign_signers_and_never_claims_authenticity_unpinned(sig, pinned, gate):
    assert binmod.signature_gate(sig, pinned)[0] == gate


def test_an_invalid_signature_refuses_the_native_call(settings, monkeypatch):
    from aeos.claude_bridge import bridge
    from aeos.claude_bridge.facade import ClaudeRequest
    from tests.unit.test_claude_bridge_run import PROMPT, SCHEMA, _probe

    called: list[int] = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    monkeypatch.setattr(binmod, "invocation_signature",
                        lambda path, fingerprint_value: {"status": "INVALID", "signer": ANTHROPIC})
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                                              domain="PERSONAL"), settings=settings)
    assert not res.ok and res.error_code == "BINARY_SIGNATURE_BLOCKED" and "manual package" in res.error
    assert not called, "the binary is never started"


def test_status_reports_blocked_or_degraded_from_the_signature(settings, db, monkeypatch):
    from aeos.claude_bridge import bridge
    from tests.unit.test_claude_bridge_run import _probe

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    for os_sig, expected in (({"status": "INVALID", "signer": ANTHROPIC}, "BLOCKED"),
                             ({"status": "UNSIGNED", "signer": None}, "DEGRADED"),
                             ({"status": "VALID", "signer": ANTHROPIC}, "CONFIGURED")):
        monkeypatch.setattr(binmod, "publisher_signature", lambda path, fingerprint_value=None, s=os_sig: s)
        out = bridge.status(db, settings, refresh=True)
        assert out["bridge_status"] == expected, os_sig
    assert out["binary"]["authenticity"]["gate"] == "UNVERIFIED"  # VALID but no pinned publisher
    assert "approved_signer" in out["binary"]["authenticity"]["owner_step"]


def test_the_owner_pinned_publisher_is_read_from_the_install_manifest():
    from aeos.inventory import facade as inventory

    assert inventory.approved_signer("claude-code") is None  # owner step: not pinned in the reviewed manifest
    assert inventory.approved_signer("no-such-component") is None


def test_a_pinned_publisher_in_the_manifest_reaches_the_gate(monkeypatch):
    from types import SimpleNamespace

    from aeos.inventory import manifest

    data = {"components": [{"component_id": "claude-code", "signature": {"method": "authenticode", "result": "UNKNOWN",
                                                                         "approved_signer": "Anthropic, PBC"}}]}
    monkeypatch.setattr(manifest, "load_manifest", lambda *a, **k: SimpleNamespace(data=data))
    assert binmod.approved_publisher() == "Anthropic, PBC"
    assert binmod.signature_gate({"status": "VALID", "signer": "CN=Other"}, binmod.approved_publisher())[0] == "BLOCKED"
