"""Official binary authenticity report (master §12 "use the unmodified official Claude Code binary")."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aeos.claude_bridge import binary as binmod

_REAL_PUBLISHER_PIN = binmod.publisher_pin  # captured at import, before the conftest stub runs


@pytest.fixture(autouse=True)
def _real_publisher_pin(monkeypatch):
    """This module tests the publisher pin itself: undo the conftest NOT_PINNED stub, so the pin is read from the
    reviewed manifest (or the manifest a test installs with _pinned)."""
    monkeypatch.setattr(binmod, "publisher_pin", _REAL_PUBLISHER_PIN)


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


@pytest.mark.parametrize("verify,detail,expected", [
    ((0, "", ""), (0, "", "Authority=Developer ID Application\nTeamIdentifier=Q6L2SF6YDW"), "VALID"),
    ((1, "", "/x/claude: code object is not signed at all"), (1, "", "code object is not signed at all"), "UNSIGNED"),
    ((1, "", "/x/claude: invalid signature (code or signature have been modified)"), (0, "", "TeamIdentifier=X"),
     "INVALID"),
    ((1, "", "/x/claude: a sealed resource is missing or invalid"), (0, "", ""), "INVALID"),
    # found on the macOS CI runner: a path that is not there is not a tampered build
    ((1, "", "claude.exe: No such file or directory"), (1, "", "claude.exe: No such file or directory"), "UNKNOWN"),
    ((1, "", "/x/claude: Permission denied"), (1, "", ""), "UNKNOWN"),
    ((2, "", "Usage: codesign -s identity [-fv*]"), (2, "", ""), "UNKNOWN"),
])
def test_macos_codesign_failures_are_invalid_only_when_the_signature_is_broken(monkeypatch, verify, detail,
                                                                               expected):
    monkeypatch.setattr(binmod.sys, "platform", "darwin")
    sig = binmod.publisher_signature(Path("/x/claude"), run=_runner({"--verify": verify, "-dv": detail}))
    assert sig["method"] == "codesign" and sig["status"] == expected
    if expected == "UNKNOWN":
        assert sig["reason"] and binmod.signature_gate(sig, None)[0] == "UNVERIFIED"  # never BLOCKED for this
    if expected == "VALID":
        assert sig["signer"] == "Q6L2SF6YDW"


def test_missing_binary_or_failing_check_is_unknown_never_valid():
    assert binmod.publisher_signature(None)["status"] == "UNKNOWN"

    def boom(argv, timeout=30):
        raise OSError("no shell")

    assert binmod.publisher_signature(Path("claude"), run=boom)["status"] == "UNKNOWN"


PINNED_TEAM = "Q6L2SF6YDW"  # the reviewed darwin publisher pin (OWNER_ACTIONS 1.12)
VALID_PINNED = {"--verify": (0, "", ""), "-dv": (0, "", f"Authority=Developer ID Application\nTeamIdentifier={PINNED_TEAM}")}


def _process_runner(monkeypatch, responses: dict) -> dict:
    """Fake the default runner, whose results publisher_signature caches for the process (an explicit run= never is),
    on a macOS host with an empty cache. Returns the mutable state: swap state["responses"] between calls."""
    import subprocess

    monkeypatch.setattr(binmod.sys, "platform", "darwin")
    monkeypatch.setattr(binmod, "_SIG_CACHE", {})
    monkeypatch.setattr(binmod, "_SIG_LAST", {})
    state: dict = {"responses": responses, "calls": 0}

    def run(argv, timeout=30.0):
        state["calls"] += 1
        step = state["responses"]
        if step == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if step == "oserror":
            raise OSError("codesign could not start")
        return next((v for k, v in step.items() if k in " ".join(argv)), (1, "", ""))

    monkeypatch.setattr(binmod, "_run", run)
    return state


@pytest.mark.parametrize("transient", [
    "timeout",                                                        # codesign hung past its 30 s timeout
    "oserror",                                                        # codesign could not be started
    {"--verify": (1, "", "/x/claude: Permission denied"), "-dv": (1, "", "")},   # UNKNOWN: says nothing of the sig
    {"--verify": (0, "", ""), "-dv": (1, "", "")},                    # VALID, but the signer was not read
])
def test_a_transient_signature_failure_is_not_cached_so_a_pinned_publisher_is_not_blocked_for_the_process(
        monkeypatch, transient):
    """With the darwin publisher pinned, a failed check BLOCKS the native call (unknown is not the publisher). That is
    right for that call, but caching it per (path, fingerprint) kept the bridge BLOCKED until the process restarted."""
    state = _process_runner(monkeypatch, transient)
    first = binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-1")
    assert binmod.signature_gate(first, PINNED_TEAM)[0] == "BLOCKED"
    # the status view (no refresh) shows that last undecided result and why, never "not checked yet"
    assert binmod.last_signature(Path("/x/claude"), "fp-1") is first
    assert binmod.last_signature(Path("/x/claude"), "fp-2") is None  # near miss: another binary was never checked
    state["responses"] = VALID_PINNED
    second = binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-1")
    assert binmod.signature_gate(second, PINNED_TEAM)[0] == "OK", (
        f"a transient signature failure was cached for the process: {second}")
    calls = state["calls"]
    assert binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-1") is second
    assert state["calls"] == calls, "a definitive VALID report is cached"


@pytest.mark.parametrize(("definite", "status"), [
    ({"--verify": (0, "", ""), "-dv": (0, "", "TeamIdentifier=FOREIGN123")}, "VALID"),     # signer mismatch
    ({"--verify": (1, "", "code object is not signed at all"), "-dv": (1, "", "not signed")}, "UNSIGNED"),
    ({"--verify": (1, "", "invalid signature (code or signature have been modified)"),
      "-dv": (0, "", f"TeamIdentifier={PINNED_TEAM}")}, "INVALID"),
])
def test_a_definitive_signature_verdict_stays_cached_and_blocked(monkeypatch, definite, status):
    """Near miss: a definite mismatch, unsigned or broken signature is the OS's verdict on that exact binary; it stays
    cached (and BLOCKED) for that fingerprint, and only a different binary (new fingerprint) is checked again."""
    state = _process_runner(monkeypatch, definite)
    first = binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-1")
    assert first["status"] == status and binmod.signature_gate(first, PINNED_TEAM)[0] == "BLOCKED"
    calls = state["calls"]
    state["responses"] = VALID_PINNED
    again = binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-1")
    assert again is first and state["calls"] == calls and binmod.signature_gate(again, PINNED_TEAM)[0] == "BLOCKED"
    swapped = binmod.publisher_signature(Path("/x/claude"), fingerprint_value="fp-2")
    assert binmod.signature_gate(swapped, PINNED_TEAM)[0] == "OK"


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

    _pinned(monkeypatch, None)  # the unpinned behaviour (the reviewed manifest pins darwin)
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

    # OWNER_ACTIONS 1.12 (2026-10-27): darwin pinned to the TeamIdentifier; windows not recorded yet (owner step)
    assert inventory.approved_signer("claude-code", os_name="darwin") == TEAM
    assert inventory.approved_signer("claude-code", os_name="windows") is None
    assert inventory.approved_signer("no-such-component") is None


@pytest.mark.parametrize(("os_name", "sig", "gate"), [
    ("darwin", {"status": "VALID", "signer": "Q6L2SF6YDW"}, "OK"),          # the official build
    ("darwin", {"status": "VALID", "signer": "ABCDE12345"}, "BLOCKED"),     # foreign-signed
    ("darwin", {"status": "UNSIGNED", "signer": None}, "BLOCKED"),          # un-signed
    ("darwin", {"status": "NOT_CHECKED"}, "BLOCKED"),                       # not proven
    ("windows", {"status": "VALID", "signer": "CN=Anyone"}, "UNVERIFIED"),  # windows NOT_RECORDED: never borrowed
])
def test_the_reviewed_manifest_pin_blocks_a_foreign_or_unsigned_claude_on_macos(monkeypatch, os_name, sig, gate):
    """Bite test of the real manifest pin (OWNER_ACTIONS 1.12): on macOS only a VALID signature by the pinned
    TeamIdentifier passes; on Windows nothing is pinned yet, so authenticity stays UNVERIFIED (never OK)."""
    from aeos.inventory import pins

    monkeypatch.setattr(pins, "host_os", lambda: os_name)
    pin = binmod.publisher_pin()
    assert pin["status"] == ("PINNED" if os_name == "darwin" else "NOT_PINNED")
    assert binmod.signature_gate(sig, pin["signer"], pin_problem=binmod.pin_error(pin))[0] == gate


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
TEAM = "Q6L2SF6YDW"  # Anthropic PBC TeamIdentifier, pinned for darwin in the reviewed manifest (OWNER_ACTIONS 1.12)


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
    binmod._SIG_LAST.clear()
    assert bridge.status(db, settings)["binary"]["authenticity"]["gate"] == "NOT_CHECKED"


def test_doctor_claude_binary_applies_the_same_gate(settings, db, monkeypatch):
    from tests.unit.test_doctor_checks import NATIVE, _ctx, check, claude_runner

    _pinned(monkeypatch, None)  # starts unpinned (the reviewed manifest pins darwin)
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


def test_an_unreadable_manifest_is_an_unknown_pin_and_blocks_never_unpinned(settings, db, monkeypatch):
    """Fail closed: when the reviewed install-manifest cannot be read/validated, whether a publisher is pinned is
    UNKNOWN - the bridge, the status page and the Doctor block (with a manifest remedy), even for a VALID binary."""
    from aeos.claude_bridge import bridge
    from aeos.claude_bridge.facade import ClaudeRequest
    from aeos.core.errors import ValidationFailed
    from aeos.inventory import manifest
    from tests.unit.test_claude_bridge_run import PROMPT, SCHEMA, _probe
    from tests.unit.test_doctor_checks import NATIVE, _ctx, check, claude_runner

    def broken(*_a, **_k):
        raise ValidationFailed("install manifest failed validation (1 errors)", code="MANIFEST_INVALID")

    monkeypatch.setattr(manifest, "load_manifest", broken)
    pin = binmod.publisher_pin()
    assert pin["status"] == "UNREADABLE" and pin["signer"] is None and "MANIFEST_INVALID" in pin["reason"]
    valid = {"status": "VALID", "signer": REAL_WINDOWS}
    assert binmod.signature_gate(valid, None, pin_problem=binmod.pin_error(pin))[0] == "BLOCKED"
    # the OS signature report says the pin is UNKNOWN, never that none exists (integration of G with main's codesign fix)
    text = binmod.publisher_signature(Path("claude-unreadable-pin"), run=lambda argv, timeout=30: (1, "", ""))
    assert text["publisher_pin"].startswith("UNKNOWN (install-manifest unreadable") and "NONE" not in text["publisher_pin"]

    called: list[int] = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    monkeypatch.setattr(binmod, "invocation_signature", lambda path, fingerprint_value: valid)
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                                              domain="PERSONAL"), settings=settings)
    assert not res.ok and res.error_code == "BINARY_SIGNATURE_BLOCKED" and not called

    monkeypatch.setattr(binmod, "publisher_signature", lambda path, fingerprint_value=None: valid)
    out = bridge.status(db, settings, refresh=True)
    au = out["binary"]["authenticity"]
    assert out["bridge_status"] == "BLOCKED" and out["blocked_by"] == ["signature"]
    assert au["gate"] == "BLOCKED" and au["pin_status"] == "UNREADABLE" and au["owner_step"] is None
    assert any("aeos.inventory.seed" in c for c in au["remedy"]["commands"]) and "/login" not in str(au["remedy"])

    res = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert res.status == "FAILED" and res.error_code == "BINARY_SIGNATURE_BLOCKED"
    assert res.observed_evidence["authenticity"]["pin_status"] == "UNREADABLE"
    assert "install-manifest" in res.remediation["operation"] and "login" not in res.remediation["operation"].lower()


def test_the_manifest_note_no_longer_claims_the_signature_is_unchecked():
    from aeos.inventory.manifest import load_manifest

    comp = next(c for c in load_manifest().data["components"] if c["component_id"] == "claude-code")
    # read with codesign on the owner's Mac (OWNER_ACTIONS 1.12); windows stays an owner step (never invented)
    assert comp["signature"]["approved_signer"] == {"darwin": TEAM}
    assert "tidak diperiksa oleh AEOS" not in (comp["signature"]["note"] or "")
    assert comp["signature"]["method"] == "os_codesign"
