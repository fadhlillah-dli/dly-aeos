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
