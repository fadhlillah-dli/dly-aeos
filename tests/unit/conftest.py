"""Unit-test isolation from the developer's machine.

The real secret stores (OS keyring, a real OpenBao on loopback) are blocked for every test by the SecretGuard in
tests/conftest.py (LESSONS L20). Here every unit test additionally gets its own empty ``MemoryKeyring`` that it can
fill, and the developer's OpenCode install is hidden on request.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest

from tests.unit.test_routing_fakes import MemoryKeyring


@pytest.fixture(autouse=True)
def _isolated_os_keyring() -> Iterator[MemoryKeyring]:
    """Every unit test starts with an empty in-memory keyring; tests that need contents set them on it (or use
    the memory_keyring fixture). The real OS keyring is never read or written."""
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    try:
        yield kr
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def opencode_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCode is not installed, as on CI, even when the developer machine has it on PATH."""
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "MISSING", "evidence": "opencode not on PATH (test)"})
