"""Bite tests of the SecretGuard in tests/conftest.py (LESSONS L20, AGENTS.md invariant 12).

Incident: on the owner's Mac a test resolved the owner's real GitHub token through the real OpenBao (AppRole login
from the macOS Keychain) and called the live GitHub API; CI has no vault and stayed green. Here a sentinel "owner
machine" stands in for that Mac: a vault on a free loopback port that serves a sentinel token, and a keyring holding
the sentinel AppRole login. Without the guard the sentinel value IS read; with the guard the read fails exactly as on
a machine without a vault. Two more doors of the owner's machine: a sandboxed child process (the containment probe)
that looked up the OS keyring itself, and the owner's real Claude Code, whose ``auth status`` reads its login from the
OS credential store. Nothing here touches the real keyring, a real vault or the real Claude Code.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import keyring
import pytest

from aeos.core import config as config_mod
from aeos.core.errors import Blocked
from aeos.delivery.provider import github_token
from aeos.inventory.probes import SystemProbeRunner
from aeos.secrets import facade
from aeos.secrets.provider import KeyringBootstrapStore, keyring_backend, reset_provider_cache
from aeos.worker_runtime import processes
from tests.conftest import (
    NULL_KEYRING,
    REAL_SECRETS_OPT_IN,
    HermeticKeyring,
    SecretGuard,
    exe_key,
    lifted_secret_guard,
)

SENTINEL_VALUE = "sentinel-owner-github-value-7f3a"  # what the owner's vault would return; never a real secret
SENTINEL_ROLE = ("sentinel-role-id-01", "sentinel-secret-id-01")
SENTINEL_TOKEN = "sentinel-vault-token-01"


class _SentinelVault(BaseHTTPRequestHandler):
    """The few OpenBao endpoints a secret read uses: health, AppRole login, KV v2 read of personal/github."""

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        if path == "/v1/sys/health":
            self._send(200, {"initialized": True, "sealed": False, "standby": False, "version": "sentinel"})
        elif path == "/v1/aeos/data/personal/github" and self.headers.get("X-Vault-Token") == SENTINEL_TOKEN:
            self._send(200, {"data": {"data": {"value": SENTINEL_VALUE}, "metadata": {}}})
        else:
            self._send(404, {"errors": []})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if urlsplit(self.path).path == "/v1/auth/approle/login" and (body.get("role_id"),
                                                                      body.get("secret_id")) == SENTINEL_ROLE:
            self._send(200, {"auth": {"client_token": SENTINEL_TOKEN, "lease_duration": 3600, "renewable": False,
                                      "policies": ["aeos-personal"]}})
        else:
            self._send(403, {"errors": ["permission denied"]})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        return


@dataclass
class OwnerMachine:
    port: int
    bootstrap_service: str


@pytest.fixture
def owner_machine(settings, monkeypatch) -> Iterator[OwnerMachine]:
    """A sentinel owner machine: a vault on a free loopback port (the configured openbao_addr) and the active OS
    keyring holding its AppRole login, as `aeos vault init` leaves them."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SentinelVault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    monkeypatch.setenv("AEOS_OPENBAO_ADDR", f"http://127.0.0.1:{port}")
    config_mod.reset_settings_cache()
    reset_provider_cache()
    service = f"{config_mod.get_settings().keyring_service}-bootstrap"
    sentinel_keyring = HermeticKeyring()
    sentinel_keyring.set_password(service, KeyringBootstrapStore.ROLE_ID, SENTINEL_ROLE[0])
    sentinel_keyring.set_password(service, KeyringBootstrapStore.SECRET_ID, SENTINEL_ROLE[1])
    previous = keyring.get_keyring()
    keyring.set_keyring(sentinel_keyring)
    try:
        yield OwnerMachine(port, service)
    finally:
        keyring.set_keyring(previous)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        reset_provider_cache()


def _read() -> str:
    return facade.get_secret("personal/github", purpose="personal:broker.github", actor="test")


def test_guard_hides_the_owner_vault_and_keyring(owner_machine: OwnerMachine):
    # guard off for this vault: the sentinel owner secret IS read, through the path of the incident
    assert github_token("PERSONAL") == (SENTINEL_VALUE, "AVAILABLE")
    guard = SecretGuard(frozenset({owner_machine.port}))
    guard.install()
    try:
        # the provider cache already holds a logged-in client: install() drops it, so the read fails as on CI
        with pytest.raises(Blocked) as exc:
            _read()
        assert exc.value.code == "SECRET_STORE_UNAVAILABLE"
        assert f"OpenBao unreachable at 127.0.0.1:{owner_machine.port}" in exc.value.message
        assert "no ACTIVE owner EXCEPTION decision" in exc.value.message
        assert github_token("PERSONAL") == (None, "MISSING: SECRET_STORE_UNAVAILABLE")
        assert keyring.get_password(owner_machine.bootstrap_service, KeyringBootstrapStore.ROLE_ID) is None
        with pytest.raises(ConnectionRefusedError, match="tests never reach a real OpenBao"):
            socket.create_connection(("127.0.0.1", owner_machine.port), timeout=2)
    finally:
        guard.uninstall()
    # uninstalled: the same machine serves the sentinel again, so the guard alone made the difference
    assert _read() == SENTINEL_VALUE


def test_keyring_layer_alone_stops_the_vault_login(owner_machine: OwnerMachine):
    """The vault stays reachable (no port guarded); hiding the keyring alone leaves no AppRole login."""
    guard = SecretGuard(frozenset())
    guard.install()
    try:
        with pytest.raises(Blocked) as exc:
            _read()
        assert exc.value.code == "SECRET_STORE_UNAVAILABLE"
        assert "OpenBao AppRole credentials are missing from the OS keyring" in exc.value.message
    finally:
        guard.uninstall()


def test_vault_layer_alone_stops_the_read(owner_machine: OwnerMachine):
    """Even with the AppRole login present in the (guard's) keyring, the guarded vault port is not reached."""
    guard = SecretGuard(frozenset({owner_machine.port}))
    guard.install()
    try:
        guard.keyring.set_password(owner_machine.bootstrap_service, KeyringBootstrapStore.ROLE_ID, SENTINEL_ROLE[0])
        guard.keyring.set_password(owner_machine.bootstrap_service, KeyringBootstrapStore.SECRET_ID, SENTINEL_ROLE[1])
        with pytest.raises(Blocked, match=f"OpenBao unreachable at 127.0.0.1:{owner_machine.port}"):
            _read()
    finally:
        guard.uninstall()


def test_session_guard_blocks_the_real_vault_address(_hermetic_secret_stores: SecretGuard):
    """The session guard is installed and refuses the default vault address in every loopback spelling, without
    opening a connection; a neighbouring port and a non-loopback host are not its business (near misses)."""
    guard = _hermetic_secret_stores
    assert socket.socket.connect.__name__ == "guarded_connect"
    default = config_mod.Settings.model_fields["openbao_addr"].default
    port = urlsplit(default).port
    assert port in guard.vault_ports
    for host in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1", "127.0.0.2", "0.0.0.0"):
        assert guard.refuses((host, port)), host
        assert guard.refuses((host, port, 0, 0)), host
    assert not guard.refuses(("127.0.0.1", port + 1))
    assert not guard.refuses(("192.0.2.10", port))
    assert not guard.refuses("/tmp/some.sock")


def test_near_miss_secrets_memory_still_supplies_a_value(settings, secrets_memory):
    secrets_memory.put("personal/github", "fake-test-github-value-01", actor="test")
    assert github_token("PERSONAL") == ("fake-test-github-value-01", "AVAILABLE")


def test_near_miss_a_test_keyring_works_and_counts_as_a_store(_hermetic_secret_stores: SecretGuard):
    """A test can still put values into a keyring it installs; the hermetic keyring is an available store (like
    Windows Credential Manager on CI), not a fail/null backend."""
    previous = keyring.get_keyring()
    kr = HermeticKeyring()
    keyring.set_keyring(kr)
    try:
        assert keyring_backend()[0] is True
        keyring.set_password("dly-aeos-test", "probe", "fake-probe-value")
        assert keyring.get_password("dly-aeos-test", "probe") == "fake-probe-value"
    finally:
        keyring.set_keyring(previous)


# ------------------------------------------------------------------ sandboxed children (containment probe)
# The child only reads its keyring configuration (environment variable, then keyringrc.cfg); it never calls a store.
_CHILD_KEYRING = ("import keyring.core as c; k = c.load_env() or c.load_config(); "
                  "print(type(k).__module__ + '.' + type(k).__name__ if k else 'OS-KEYRING-DETECTION')")


def _sandbox_child_keyring(root: Path) -> str:
    """What a child started like the containment probe (sandbox environment, ``python -I``) would use as keyring."""
    dirs = processes.SandboxDirs.create(root)
    env = processes.build_sandbox_env(dirs, run_id="keyring-bite")
    out = subprocess.run([sys.executable, "-I", "-c", _CHILD_KEYRING], env=env, cwd=str(dirs.root),
                         capture_output=True, text=True, timeout=120, check=True)
    return out.stdout.strip()


def test_guard_gives_sandboxed_children_the_null_keyring(_hermetic_secret_stores: SecretGuard, tmp_path: Path):
    """The sandbox environment drops every KEYRING variable, so without the guard the probe child falls through to
    OS keyring detection (the macOS Keychain, Windows Credential Manager); with it, the sandbox home selects null."""
    assert _sandbox_child_keyring(tmp_path / "guarded") == NULL_KEYRING
    _hermetic_secret_stores.uninstall()
    try:
        assert _sandbox_child_keyring(tmp_path / "unguarded") == "OS-KEYRING-DETECTION"
    finally:
        _hermetic_secret_stores.install()
    assert _sandbox_child_keyring(tmp_path / "again") == NULL_KEYRING


# ------------------------------------------------------------------ the owner's real Claude Code
_SENTINEL_LOGIN = "sentinel-claude-login-loggedIn"


def test_guard_refuses_to_start_the_owner_claude(tmp_path: Path):
    """The sentinel "real Claude Code" is this interpreter printing a sentinel login, started through the doctor's
    probe runner. The session guard does not know it, so only the guard under test makes the difference."""
    argv = [sys.executable, "-c", f"print('{_SENTINEL_LOGIN}')"]
    runner = SystemProbeRunner()
    assert runner.run(argv, timeout=120).stdout.strip() == _SENTINEL_LOGIN
    guard = SecretGuard(frozenset(), frozenset({exe_key(sys.executable)}))
    guard.install()
    try:
        res = runner.run(argv, timeout=120)
        assert (res.returncode, res.stdout, res.error) == (None, "", "NOT_FOUND")
        with pytest.raises(FileNotFoundError, match="tests never run the owner's real Claude Code"):
            subprocess.run(argv, capture_output=True, check=False)
        assert guard.refuses_exec([os.path.basename(sys.executable), "auth", "status"],
                                  env={"PATH": os.path.dirname(sys.executable)})
        assert guard.refuses_exec(["any-name"], executable=sys.executable)
        # near misses: a fake claude a test writes elsewhere is another file; a test that opted in runs the real one
        fake = tmp_path / "claude"
        fake.write_text("", encoding="utf-8")
        assert not guard.refuses_exec([str(fake), "auth", "status"])
        guard.allow_real_claude = True
        assert runner.run(argv, timeout=120).stdout.strip() == _SENTINEL_LOGIN
        guard.allow_real_claude = False
    finally:
        guard.uninstall()
    assert runner.run(argv, timeout=120).stdout.strip() == _SENTINEL_LOGIN


def test_session_guard_knows_the_owner_claude(_hermetic_secret_stores: SecretGuard):
    """Whatever ``claude`` PATH finds on this machine is refused (checked without starting it); CI has none."""
    guard = _hermetic_secret_stores
    assert subprocess.Popen.__init__.__name__ == "guarded_popen_init"
    assert processes.SandboxDirs.create.__func__.__name__ == "guarded_sandbox_create"
    assert guard.allow_real_claude is False
    for name in ("claude", "claude.exe"):
        found = shutil.which(name)
        if found:
            assert exe_key(found) in guard.claude_binaries
            assert guard.refuses_exec([found, "auth", "status", "--json"])


GUARDED = "tests never reach a real OpenBao"


def _connect(port: int) -> str:
    """'connected' when the connect reaches the test's own listener, else the refusal message."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    except ConnectionRefusedError as exc:
        return str(exc)
    return "connected"


def test_bite_the_real_secret_stores_opt_in_skips_without_the_owner_variable_and_restores_the_guard():
    # the test's own listener on a free loopback port that only this guard blocks (the session guard does not): it
    # holds the port for the whole test, so no other process can take it, and a connect with the guard off succeeds
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    guard = SecretGuard(frozenset({port}))
    guard.install()
    try:
        assert GUARDED in _connect(port)
        with pytest.raises(pytest.skip.Exception, match=f"{REAL_SECRETS_OPT_IN}=1"), lifted_secret_guard(guard, {}):
            pass  # pragma: no cover - never entered
        # only the exact value "1" opts in
        with pytest.raises(pytest.skip.Exception), lifted_secret_guard(guard, {REAL_SECRETS_OPT_IN: "yes"}):
            pass  # pragma: no cover
        assert GUARDED in _connect(port)  # a skip leaves the guard on
        with lifted_secret_guard(guard, {REAL_SECRETS_OPT_IN: "1"}):
            assert _connect(port) == "connected"  # off inside the block: the listener answers
        assert GUARDED in _connect(port)  # and on again afterwards
        with pytest.raises(RuntimeError), lifted_secret_guard(guard, {REAL_SECRETS_OPT_IN: "1"}):
            raise RuntimeError("the opted-in test failed")
        assert GUARDED in _connect(port)  # a failing test re-installs it too
    finally:
        guard.uninstall()
        listener.close()


def test_bite_the_real_secret_stores_fixture_reads_the_real_environment(request, monkeypatch):
    """The fixture's wiring: it reads os.environ (not a fixed mapping), so without the owner's variable it skips."""
    monkeypatch.delenv(REAL_SECRETS_OPT_IN, raising=False)
    with pytest.raises(pytest.skip.Exception, match=f"{REAL_SECRETS_OPT_IN}=1"):
        request.getfixturevalue("real_secret_stores")
