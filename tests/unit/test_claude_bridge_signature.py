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
    # W6-042: once a publisher is pinned, the binary must be PROVEN to be its build - anything but VALID blocks
    (None, "Anthropic", "BLOCKED"),
    ({"status": "UNSIGNED", "signer": None}, "Anthropic, PBC", "BLOCKED"),
    ({"status": "UNKNOWN"}, "Anthropic, PBC", "BLOCKED"),
    ({"status": "NOT_CHECKED"}, "Anthropic, PBC", "BLOCKED"),
])
def test_signature_gate_blocks_invalid_or_foreign_signers_and_never_claims_authenticity_unpinned(sig, pinned, gate):
    assert binmod.signature_gate(sig, pinned)[0] == gate


QUOTED = 'CN="Anthropic, PBC", O="Anthropic, PBC", L=San Francisco, S=California, C=US'
# The subject Get-AuthenticodeSignature reports for the winget-installed claude.exe (observed 2026-10-25).
REAL_WINDOWS = ('CN="Anthropic, PBC", O="Anthropic, PBC", L=San Francisco, S=California, C=US, SERIALNUMBER=4860621, '
                'OID.2.5.4.15=Private Organization, OID.1.3.6.1.4.1.311.60.2.1.2=Delaware, '
                'OID.1.3.6.1.4.1.311.60.2.1.3=US')


@pytest.mark.parametrize(("signer", "pinned", "gate"), [
    # a pin is an exact identity, never a substring of the observed subject
    ("CN=Anthropic, PBC Labs, O=Anthropic, PBC Labs, C=US", "Anthropic, PBC", "BLOCKED"),
    ("CN=Not Anthropic, PBC, O=Evil Inc, C=US", "Anthropic, PBC", "BLOCKED"),
    (ANTHROPIC, "Anthropic", "BLOCKED"),
    ("Q6L2SF6YDWX", "Q6L2SF6YDW", "BLOCKED"),  # macOS TeamIdentifier: exact
    # the exact CN / O value, a whole RDN or the whole subject match (quoted or unquoted DN form)
    (QUOTED, "Anthropic, PBC", "OK"),
    (QUOTED, "O=Anthropic, PBC", "OK"),
    (REAL_WINDOWS, "Anthropic, PBC", "OK"),
    (REAL_WINDOWS, "SERIALNUMBER=4860621", "OK"),
    (REAL_WINDOWS, "4860621", "BLOCKED"),  # a bare non-CN/O value is not an identity
    (ANTHROPIC, "o=anthropic,  pbc", "OK"),
    (ANTHROPIC, ANTHROPIC, "OK"),
    ("Q6L2SF6YDW", "Q6L2SF6YDW", "OK"),
])
def test_the_pinned_publisher_must_match_exactly_never_as_a_substring(signer, pinned, gate):
    assert binmod.signature_gate({"status": "VALID", "signer": signer}, pinned)[0] == gate


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
    # the OS report names the real pin instead of claiming that none exists

    def run(argv, timeout=30):
        return (1, "", "")

    assert binmod.publisher_signature(Path("claude"), run=run)["publisher_pin"].startswith("Anthropic, PBC")


# ------------------------------------------------------------------------------------------------- W6-042
TEAM = "Q6L2SF6YDW"  # an example TeamIdentifier shape (tests only; the owner records the real one)


def _pinned(monkeypatch, approved_signer) -> None:
    from types import SimpleNamespace

    from aeos.inventory import manifest

    data = {"components": [{"component_id": "claude-code", "signature": {
        "method": "os_codesign", "result": "UNKNOWN", "approved_signer": approved_signer}}]}
    monkeypatch.setattr(manifest, "load_manifest", lambda *a, **k: SimpleNamespace(data=data))


@pytest.mark.parametrize(("os_name", "observed", "gate"), [
    ("windows", REAL_WINDOWS, "OK"), ("darwin", TEAM, "OK"),
    ("windows", TEAM, "BLOCKED"), ("darwin", REAL_WINDOWS, "BLOCKED"),  # never the other OS's identity
])
def test_a_per_os_pin_covers_windows_and_macos(monkeypatch, os_name, observed, gate):
    from aeos.inventory import pins

    _pinned(monkeypatch, {"windows": "Anthropic, PBC", "darwin": TEAM})
    monkeypatch.setattr(pins, "host_os", lambda: os_name)
    assert binmod.approved_publisher() == ("Anthropic, PBC" if os_name == "windows" else TEAM)
    assert binmod.signature_gate({"status": "VALID", "signer": observed}, binmod.approved_publisher())[0] == gate


def test_an_os_without_a_pin_entry_is_unverified_not_borrowed(monkeypatch):
    from aeos.inventory import pins

    _pinned(monkeypatch, {"windows": "Anthropic, PBC"})
    monkeypatch.setattr(pins, "host_os", lambda: "darwin")
    assert binmod.approved_publisher() is None
    assert binmod.signature_gate({"status": "VALID", "signer": TEAM}, binmod.approved_publisher())[0] == "UNVERIFIED"


def test_a_pinned_publisher_blocks_an_unsigned_binary_before_the_native_call(settings, monkeypatch):
    from aeos.claude_bridge import bridge
    from aeos.claude_bridge.facade import ClaudeRequest
    from tests.unit.test_claude_bridge_run import PROMPT, SCHEMA, _probe

    _pinned(monkeypatch, "Anthropic, PBC")
    called: list[int] = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    for sig in ({"status": "UNSIGNED", "signer": None}, {"status": "UNKNOWN", "signer": None}):
        monkeypatch.setattr(binmod, "invocation_signature", lambda path, fingerprint_value, s=sig: s)
        res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT,
                                                  json_schema=SCHEMA, domain="PERSONAL"), settings=settings)
        assert not res.ok and res.error_code == "BINARY_SIGNATURE_BLOCKED", sig
    assert not called


def test_status_names_the_authenticity_gate_and_a_signature_remedy(settings, db, monkeypatch):
    from aeos.claude_bridge import bridge
    from tests.unit.test_claude_bridge_run import _probe

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    _pinned(monkeypatch, "Anthropic, PBC")
    monkeypatch.setattr(binmod, "publisher_signature", lambda path, fingerprint_value=None: {"status": "UNSIGNED"})
    out = bridge.status(db, settings, refresh=True)
    au = out["binary"]["authenticity"]
    assert out["bridge_status"] == "BLOCKED" and out["blocked_by"] == ["signature"]
    assert au["gate"] == "BLOCKED" and au["approved_signer"] == "Anthropic, PBC" and au["owner_step"] is None
    assert au["remedy"] and "/login" not in str(au["remedy"]), "a signature block is never fixed by /login"
    _pinned(monkeypatch, None)
    out = bridge.status(db, settings, refresh=True)
    assert out["bridge_status"] == "DEGRADED" and out["binary"]["authenticity"]["gate"] == "DEGRADED"
    assert out["binary"]["authenticity"]["remedy"]
    # not refreshed: the OS check has not run in this process -> NOT_CHECKED, never authentic
    binmod._SIG_CACHE.clear()
    assert bridge.status(db, settings)["binary"]["authenticity"]["gate"] == "NOT_CHECKED"


def test_doctor_claude_binary_applies_the_same_gate(settings, db, monkeypatch):
    from tests.unit.test_doctor_checks import NATIVE, _ctx, check, claude_runner

    monkeypatch.setattr(binmod, "invocation_signature",
                        lambda path, fingerprint_value: {"status": "INVALID", "signer": ANTHROPIC})
    res = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert res.status == "FAILED" and res.error_code == "BINARY_SIGNATURE_BLOCKED"
    assert res.observed_evidence["authenticity"]["gate"] == "BLOCKED"
    assert "login" not in res.remediation["operation"].lower() and "approved_signer" in res.remediation["operation"]
    assert res.observations[0]["status"] == "INSTALLED"  # installed, but not authentic

    monkeypatch.setattr(binmod, "invocation_signature", lambda path, fingerprint_value: {"status": "UNSIGNED"})
    res = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert res.status == "VERIFIED" and res.observed_evidence["authenticity"]["gate"] == "DEGRADED"
    assert "DEGRADED" in res.summary and res.observed_evidence["distribution_verified"] is False

    _pinned(monkeypatch, "Anthropic, PBC")
    res = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert res.status == "FAILED" and res.error_code == "BINARY_SIGNATURE_BLOCKED"  # pinned + unsigned
    monkeypatch.setattr(binmod, "invocation_signature",
                        lambda path, fingerprint_value: {"status": "VALID", "signer": REAL_WINDOWS})
    res = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert res.status == "VERIFIED" and res.observed_evidence["authenticity"]["gate"] == "OK"
    assert res.observed_evidence["distribution_verified"] is True


def test_the_manifest_note_no_longer_claims_the_signature_is_unchecked():
    from aeos.inventory.manifest import load_manifest

    comp = next(c for c in load_manifest().data["components"] if c["component_id"] == "claude-code")
    assert comp["signature"]["approved_signer"] is None  # owner step: not pinned yet (never invented)
    assert "tidak diperiksa oleh AEOS" not in (comp["signature"]["note"] or "")
    assert comp["signature"]["method"] == "os_codesign"
