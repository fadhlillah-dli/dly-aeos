"""OS credential store selection (master §26 "Pada Windows gunakan credential store OS yang sesuai"): a
fail/null keyring backend is never treated as a credential store, and vault init refuses it before any
OpenBao call is made."""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from aeos.secrets import provider, setup


class NullKeyring(KeyringBackend):
    """A backend that silently drops writes (like keyring.backends.null.Keyring)."""

    priority = 1  # type: ignore[assignment]

    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        return None

    def delete_password(self, service, username):
        return None


class GoodKeyring(NullKeyring):
    pass


@pytest.fixture
def restore_keyring():
    previous = keyring.get_keyring()
    yield
    keyring.set_keyring(previous)


@pytest.mark.parametrize("backend", [fail.Keyring(), NullKeyring()], ids=["fail", "null"])
def test_fail_and_null_backends_are_rejected(backend, restore_keyring):
    keyring.set_keyring(backend)
    ok, name = provider.keyring_backend()
    assert ok is False and ("fail" in name.lower() or "null" in name.lower())
    store = provider.KeyringBootstrapStore("dly-aeos-test")
    assert store.available()[0] is False
    with pytest.raises(setup.OpenBaoError, match="OS keyring unavailable"):
        setup.run_init("http://127.0.0.1:1", shares=1, threshold=1, bootstrap=store)  # refused before any HTTP


def test_a_real_backend_is_accepted(restore_keyring):
    keyring.set_keyring(GoodKeyring())
    ok, name = provider.keyring_backend()
    assert ok is True and name.endswith("GoodKeyring")
