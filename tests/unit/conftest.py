"""Unit-test isolation from the developer's machine.

A developer machine that runs DLY AEOS has a real OpenBao on the default loopback address and its AppRole
credentials in the real OS keyring (macOS Keychain, Windows Credential Manager). Without these fixtures a unit
test that reads the secret store logs in to that vault, and the result depends on the machine instead of the
code (CI has neither). Integration tests (tests/integration) are not affected.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import keyring
import pytest

from aeos.core.config import Settings
from tests.unit.test_routing_fakes import MemoryKeyring

_OPENBAO_DEFAULT = urlparse(Settings.model_fields["openbao_addr"].default)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


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


@pytest.fixture(autouse=True)
def _no_real_openbao(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real TCP connection to the default OpenBao address is refused, as on a machine without a vault. Tests
    that mock the address (respx, fake runners) never open a socket and are unaffected."""
    real = socket.create_connection

    def guarded(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        host, port = address[0], int(address[1])
        if port == _OPENBAO_DEFAULT.port and host in _LOOPBACK:
            raise ConnectionRefusedError(f"unit tests never reach a real OpenBao at {host}:{port}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)


@pytest.fixture
def opencode_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCode is not installed, as on CI, even when the developer machine has it on PATH."""
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "MISSING", "evidence": "opencode not on PATH (test)"})
