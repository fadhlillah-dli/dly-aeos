"""The unit-test isolation in tests/unit/conftest.py holds (machine independence, see its docstring)."""

from __future__ import annotations

import socket

import keyring
import pytest

from tests.unit.test_routing_fakes import MemoryKeyring


def test_unit_tests_never_reach_the_real_vault_or_os_keyring():
    assert isinstance(keyring.get_keyring(), MemoryKeyring) and not keyring.get_keyring().store
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", 8200), timeout=1)
