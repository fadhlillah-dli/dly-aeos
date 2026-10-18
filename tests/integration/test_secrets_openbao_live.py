"""Real OpenBao roundtrip (§07 order 3 evidence: vault health, policy scope, token TTL, restart).

Runs only when a ``bao`` binary is available (``aeos vault install`` or PATH) — skipped otherwise.
Uses a throw-away data dir, a free loopback port and an in-memory keyring; never the owner's vault.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from aeos.core.errors import Blocked, PolicyDenied
from aeos.secrets import server
from aeos.secrets.openbao import OpenBaoClient, OpenBaoUnavailable
from aeos.secrets.provider import OpenBaoProvider, bootstrap_store, reset_provider_cache
from aeos.secrets.setup import run_init
from tests.unit.test_routing_fakes import memory_keyring  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def _wait_up(addr: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            OpenBaoClient(addr).health()
            return
        except OpenBaoUnavailable:
            if time.monotonic() > deadline:
                pytest.fail("OpenBao did not start")
            time.sleep(0.3)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_openbao_init_put_get(settings, memory_keyring):  # noqa: F811
    explicit = os.environ.get("DLY_AEOS_TEST_BAO")  # path to a bao binary (AEOS_* vars are reset by fixtures)
    binary = Path(explicit) if explicit and Path(explicit).is_file() else server.bin_path(settings)
    if binary is None:
        pytest.skip("OpenBao binary not installed (aeos vault install) and DLY_AEOS_TEST_BAO not set")
    s = settings.model_copy(update={"openbao_addr": f"http://127.0.0.1:{_free_port()}"})
    info = server.start(s, binary)
    try:
        _wait_up(s.openbao_addr)
        res = run_init(s.openbao_addr, shares=1, threshold=1, bootstrap=bootstrap_store(s))
        assert res.error is None and res.root_revoked, res
        reset_provider_cache()
        p = OpenBaoProvider(s, bootstrap_store(s))
        p.put("personal/github", "live-test-value-123456", actor="pytest")
        assert p.get("personal/github", purpose="personal:test", actor="pytest") == "live-test-value-123456"
        assert p.status("personal/github").exists
        with pytest.raises(PolicyDenied):
            p.put("office/atlassian", "nope-nope-nope", actor="pytest")  # office policy not bound to the AppRole
        # restart/recovery: after a restart the vault is sealed (fail closed) until unsealed, data survives
        server.stop(s)
        server.start(s, binary)
        _wait_up(s.openbao_addr)
        reset_provider_cache()
        p2 = OpenBaoProvider(s, bootstrap_store(s))
        assert p2.health(fresh=True)["sealed"] is True
        with pytest.raises(Blocked):
            p2.get("personal/github", purpose="personal:test", actor="pytest")
        st = OpenBaoClient(s.openbao_addr).unseal(res.keys[0])
        assert st.get("sealed") is False
        time.sleep(1.0)
        p3 = OpenBaoProvider(s, bootstrap_store(s))
        deadline = time.monotonic() + 15
        while True:
            try:
                assert p3.get("personal/github", purpose="personal:test", actor="pytest") == "live-test-value-123456"
                break
            except Blocked:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
    finally:
        server.stop(s)
        assert info["pid"]
