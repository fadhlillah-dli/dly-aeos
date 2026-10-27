"""Shared test fixtures. Every test gets an isolated data directory and database.

Also the CI shard option ``--shard K/N`` (see the section at the end of this file)."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from sqlalchemy.orm import Session

from aeos.core import config as config_mod
from aeos.core.db import build_engine, create_all, init_engine, session_factory
from aeos.core.enums import Domain, Role

ADMIN_PW = "correct-horse-battery-staple"
VIEWER_PW = "read-only-observer-pass-77"
# The git identity of the test repositories (tests/unit/test_workforce_support.init_git_repo) and the code-host
# commit identity the test owner configured (placeholder addresses only: the repository is public).
TEST_GIT_NAME = "Test"
TEST_GIT_EMAIL = "test@example.invalid"


@pytest.fixture(autouse=True)
def _controller_not_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI runners (GitHub-hosted Windows) run elevated; the product refuses the process sandbox for an elevated
    controller (§25 non-root). That refusal is tested explicitly (test_worker_runtime_sandbox); every other test
    runs as a normal user would."""
    from aeos.worker_runtime import sandbox

    monkeypatch.setattr(sandbox, "_elevation_probe", lambda: False)


@pytest.fixture(autouse=True)
def _no_os_signature_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge checks the OS code signature of the claude binary before a native call (§12). Test binaries
    are fakes, so the OS check (PowerShell Authenticode / codesign) is not spawned per test; the gate itself is
    tested explicitly in test_claude_bridge_signature.py."""
    from aeos.claude_bridge import binary

    monkeypatch.setattr(binary, "invocation_signature",
                        lambda path, fingerprint_value: {"status": "NOT_CHECKED", "reason": "test stub"})


@pytest.fixture(autouse=True)
def _no_owner_publisher_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewed manifest pins the Claude Code publisher per OS (darwin: TeamIdentifier, OWNER_ACTIONS 1.12). A
    pinned publisher BLOCKS every binary whose OS signature is not VALID by that signer, by design, and the test
    binaries are fakes whose OS check is stubbed above. Other tests therefore see the publisher NOT_PINNED, as on a
    host without a pin. test_claude_bridge_signature.py restores the real pin and tests it with the gate."""
    from aeos.claude_bridge import binary

    monkeypatch.setattr(binary, "publisher_pin",
                        lambda: {"signer": None, "status": "NOT_PINNED", "reason": "test stub"})


class _MemoArgon2:
    """Wraps the production ``PasswordHasher`` so one test process pays each Argon2id computation (t=3, 64 MiB,
    p=4, tens of ms) once per distinct input instead of once per test. Every value still comes from the real hasher
    with the production parameters: ``hash`` is memoised per password (a call with an explicit salt is never
    cached), ``verify`` per (hash, password) including the argon2 failures (re-raised as a fresh exception, never
    the stored instance); any other exception is not cached. Everything else (``check_needs_rehash`` ...) is
    delegated untouched."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._hashes: dict[Any, str] = {}
        self._verdicts: dict[tuple[Any, Any], Any] = {}

    def hash(self, password: Any, *, salt: bytes | None = None) -> str:
        if salt is not None:
            return str(self._real.hash(password, salt=salt))
        cached = self._hashes.get(password)
        if cached is None:
            cached = self._hashes[password] = self._real.hash(password)
        return cached

    def verify(self, hash: Any, password: Any) -> bool:
        from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

        key = (hash, password)
        if key in self._verdicts:
            verdict = self._verdicts[key]
            if verdict is True:
                return True
            exc_type, args = verdict
            raise exc_type(*args)
        try:
            result = bool(self._real.verify(hash, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError) as exc:
            self._verdicts[key] = (type(exc), exc.args)
            raise
        if result:
            self._verdicts[key] = True
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture(scope="session", autouse=True)
def _memoised_argon2() -> Iterator[None]:
    """Fails open: if the identity module or its stock ``PasswordHasher`` is not there, nothing is wrapped."""
    try:
        from argon2 import PasswordHasher

        import aeos.identity.passwords as pw
    except ImportError:
        yield
        return
    real = getattr(pw, "_hasher", None)
    if type(real) is not PasswordHasher:
        yield
        return
    wrapper = _MemoArgon2(real)
    setattr(pw, "_hasher", wrapper)  # noqa: B010 - the wrapper is deliberately not a PasswordHasher
    try:
        yield
    finally:
        if getattr(pw, "_hasher", None) is wrapper:
            setattr(pw, "_hasher", real)  # noqa: B010


_TEMPLATE_ROOT: list[Path] = []


@pytest.fixture(scope="session", autouse=True)
def _template_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Parent of the per-process git/release template repositories. It lives under pytest's own base temp
    directory, which pytest prunes, so a killed run leaves nothing in $TMPDIR (an atexit hook never runs then)."""
    _TEMPLATE_ROOT.append(tmp_path_factory.mktemp("aeos-templates"))
    try:
        yield
    finally:
        _TEMPLATE_ROOT.clear()


def template_dir(prefix: str) -> Path:
    """A new empty directory for a template repository (valid only while the pytest session runs)."""
    if not _TEMPLATE_ROOT:
        raise RuntimeError("template_dir() is only available inside a pytest session")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=_TEMPLATE_ROOT[0]))


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One migrated database per test process (per xdist worker): create_all + the append-only triggers + the
    alembic head stamp, built once. Each test's ``settings`` gets its own copy, so every test still starts from a
    fresh, identical database. The template file itself is never opened by a test."""
    path = tmp_path_factory.mktemp("aeos-template") / "template.sqlite3"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        create_all(engine)
        _stamp_head(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _template_db: Path) -> Iterator[config_mod.Settings]:
    monkeypatch.setenv("AEOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "no-config.yaml"))
    for k in list(os.environ):
        if k.startswith("AEOS_") and k not in {"AEOS_DATA_DIR", "AEOS_CONFIG"}:
            monkeypatch.delenv(k, raising=False)
    # `aeos serve` (invoked in-process by CLI tests) writes these into os.environ; record them so that
    # teardown removes whatever such a command set and nothing leaks into later tests.
    for k in ("AEOS_PORT", "AEOS_DEV_MODE", "AEOS_HOST"):
        if k not in os.environ:
            monkeypatch.setenv(k, "")
            monkeypatch.delenv(k)
    # Owner rule (AGENTS.md invariant 11): the broker pushes only commits authored AND committed by the configured
    # code-host identity. Tests run as an owner who configured one for both hosts - the identity the test git
    # repositories commit with (init_git_repo); tests of the unset/invalid/mismatch cases override it explicitly.
    monkeypatch.setenv("AEOS_COMMIT_IDENTITIES", json.dumps({h: {"name": TEST_GIT_NAME, "email": TEST_GIT_EMAIL}
                                                             for h in ("github", "bitbucket")}))
    config_mod.reset_settings_cache()
    _reset_process_caches()
    s = config_mod.get_settings()
    engine = init_engine(s)  # also creates the data directory layout
    # sqlite's online backup copies the template page by page (through any WAL) into this test's new database file
    src = sqlite3.connect(_template_db)
    try:
        dst = sqlite3.connect(s.layout.db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    yield s
    engine.dispose()
    config_mod.reset_settings_cache()
    _reset_process_caches()


_HEAD: list[str] = []


def _stamp_head(engine) -> None:
    """Record the migration head like `aeos db upgrade` does for a create_all database (fast path)."""
    if not _HEAD:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from aeos.core.migrate import MIGRATIONS_DIR

        # script_location only: `alembic_config()` would call get_settings() and read the host's AEOS_* variables and
        # aeos.yaml, because the template is built before the `settings` fixture isolates them
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        _HEAD.append(ScriptDirectory.from_config(cfg).get_current_head() or "")
    if _HEAD[0]:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, "
                                 "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
            conn.exec_driver_sql("DELETE FROM alembic_version")
            conn.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES (?)", (_HEAD[0],))


def _reset_process_caches() -> None:
    """Process-level caches (OpenBao health/token, claude binary probe) must not leak across tests."""
    from aeos.claude_bridge.binary import clear_probe_cache
    from aeos.secrets.provider import reset_provider_cache

    reset_provider_cache()
    clear_probe_cache()


@pytest.fixture
def db(settings: config_mod.Settings) -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def secrets_memory() -> Iterator[object]:
    from aeos.secrets.provider import MemorySecretProvider, set_provider_override

    p = MemorySecretProvider()
    set_provider_override(p)
    yield p
    set_provider_override(None)


# ------------------------------------------------------------------ hermetic secret stores (LESSONS L20)
# On the owner's Mac a real OpenBao listens on loopback and its AppRole login lives in the macOS Keychain, so a test
# that resolved a secret used the owner's real GitHub token against the live API (CI has neither and stayed green).


class HermeticKeyring(KeyringBackend):
    """The OS keyring every test sees: in memory and empty, like a fresh CI runner's credential store. Not
    named fail/null, so the product treats it as an available store (as it does Windows Credential Manager)."""

    priority = 0.5  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not in the test keyring")


def _is_loopback(host: object) -> bool:
    text = str(host).strip("[]").split("%", 1)[0].lower()
    if text == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback or ip.ipv4_mapped.is_unspecified
    return ip.is_loopback or ip.is_unspecified


def real_vault_ports() -> frozenset[int]:
    """Loopback ports a real OpenBao of this machine can listen on: the default ``openbao_addr``, the
    ``AEOS_OPENBAO_ADDR`` variable and the host's aeos.yaml, read when the session starts (before the ``settings``
    fixture isolates them). Only loopback counts: the product refuses any other vault address."""
    addrs = [str(config_mod.Settings.model_fields["openbao_addr"].default), os.environ.get("AEOS_OPENBAO_ADDR", "")]
    try:
        addrs.append(config_mod.Settings().openbao_addr)
    except Exception:  # noqa: BLE001 - an unreadable host config adds no address; the default stays blocked
        pass
    ports: set[int] = set()
    for addr in addrs:
        parts = urlsplit(addr)
        if parts.hostname and _is_loopback(parts.hostname):
            ports.add(parts.port or (443 if parts.scheme == "https" else 80))
    return frozenset(ports)


def exe_key(path: str | os.PathLike[str]) -> str:
    """One spelling per executable file: symlinks, ``~`` and case (Windows) resolved."""
    return os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(path))))


def real_claude_binaries() -> frozenset[str]:
    """The owner's real Claude Code, as PATH, the ``AEOS_CLAUDE_BINARY`` variable and the host's aeos.yaml resolve
    it when the session starts. Its login (OAuth credential) lives in the OS credential store, which
    ``claude auth status`` reads. A fake ``claude`` a test writes elsewhere is a different file."""
    found: list[str | None] = [shutil.which("claude"), shutil.which("claude.exe"), os.environ.get("AEOS_CLAUDE_BINARY")]
    try:
        found.append(config_mod.Settings().claude_binary)
    except Exception:  # noqa: BLE001 - an unreadable host config adds no path; PATH and the variable still count
        pass
    return frozenset(exe_key(p) for p in found if p)


# keyring reads this file from XDG_CONFIG_HOME (macOS, Linux) or LOCALAPPDATA (Windows), both inside a sandbox home
SANDBOX_KEYRING_CONFIGS = (Path(".config", "python_keyring", "keyringrc.cfg"),
                           Path("AppData", "Local", "Python Keyring", "keyringrc.cfg"))
NULL_KEYRING = "keyring.backends.null.Keyring"


def write_sandbox_keyring_config(home: Path) -> None:
    """A child started with the sandbox environment (which drops every KEYRING variable) uses the null keyring:
    the keyring API answers "no password" and never reaches the OS credential store."""
    for rel in SANDBOX_KEYRING_CONFIGS:
        cfg = home / rel
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f"[backend]\ndefault-keyring={NULL_KEYRING}\n", encoding="utf-8")


class SecretGuard:
    """Makes the real secret stores unreachable from the test process (AGENTS.md invariant 12, LESSONS L20).

    Blocks: (a) the real OS keyring (macOS Keychain, Windows Credential Manager, Secret Service), replaced by an
    empty ``HermeticKeyring``, so the AppRole login of the owner's vault is never found; (b) a real OpenBao: a
    socket connect to a loopback address on one of ``vault_ports`` is refused as on a machine without a vault (this
    covers httpx, urllib and asyncio, which all end in ``socket.connect``); (c) the provider's process cache (an
    OpenBao client and token from before the guard); (d) the OS keyring of a sandboxed child process: every
    ``SandboxDirs.create`` also writes a keyring config selecting the null backend into the sandbox home, so the
    containment probe and worker children never query the macOS Keychain or Windows Credential Manager; (e) the
    owner's real Claude Code (``claude_binaries``), whose ``auth status`` reads its login from the OS credential
    store: starting it raises ``FileNotFoundError`` before any process exists, as if the binary had vanished, so
    every caller takes its "cannot run" path (a fake ``claude`` elsewhere still runs). Default result:
    ``get_secret`` raises ``Blocked(SECRET_STORE_UNAVAILABLE)``, the CI behaviour.

    Out of scope, with the reason: GITHUB_TOKEN/GH_TOKEN, the gh CLI and netrc (the product never reads a credential
    from them: secrets come only from the provider, and httpx 0.28 applies netrc only with an explicit
    ``NetRCAuth``); git credential helpers (the broker and the verifier run git with ``credential.helper=``
    reset); OpenCode (every probe runs it with the sandbox environment, so its auth.json resolves inside the
    sandbox home); the verifier's children (``scrubbed_env`` already selects the null keyring); the UI smoke server
    (integration only), a real uvicorn child outside any sandbox: its environment (``_isolated_env``) sets
    ``PYTHON_KEYRING_BACKEND`` to ``NULL_KEYRING``, gets an absent ``claude``, a free vault port and a random keyring
    service name, and ``test_ui_smoke_server_child_selects_the_null_keyring`` keeps it so. Not covered: a future child
    process outside a sandbox that runs its own Python and its own keyring lookup without such an environment.

    Opting in: ``secrets_memory`` (or ``set_provider_override``) supplies values; a test that needs keyring contents
    sets them on the active keyring or installs its own backend (``keyring.set_keyring``); a fake vault runs on a
    free port or behind respx/MockTransport, neither of which opens a socket to a guarded port; a test that must run
    the owner's real Claude Code requests the ``real_claude_cli`` fixture, which skips unless the owner set
    ``DLY_AEOS_REAL_CLAUDE=1`` (or ``DLY_AEOS_LIVE_CLAUDE=1``); a test that must read the owner's real secret stores
    requests ``real_secret_stores``, which skips unless the owner set ``DLY_AEOS_REAL_SECRETS=1``."""

    def __init__(self, vault_ports: frozenset[int], claude_binaries: frozenset[str] = frozenset()) -> None:
        self.vault_ports = vault_ports
        self.claude_binaries = claude_binaries
        self.allow_real_claude = False
        self.keyring = HermeticKeyring()
        self._saved: tuple[Any, ...] | None = None

    def refuses(self, address: object) -> bool:
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        try:
            port = int(address[1])
        except (TypeError, ValueError):
            return False
        return port in self.vault_ports and _is_loopback(address[0])

    def refuses_exec(self, args: Any, executable: Any = None, env: Mapping[str, str] | None = None,
                     shell: bool = False) -> bool:
        """True when ``subprocess.Popen(args, ...)`` would start the owner's real Claude Code."""
        if self.allow_real_claude or not self.claude_binaries:
            return False
        program = executable
        if program is None:
            if isinstance(args, (str, bytes, os.PathLike)):
                program = os.fsdecode(args).split()[0] if shell and os.fsdecode(args).split() else args
            elif args:
                program = args[0]
        if not program:
            return False
        text = os.fsdecode(program)
        if not os.path.dirname(text):
            path = (env if env is not None else os.environ).get("PATH")
            text = shutil.which(text, path=path) or text
        return exe_key(text) in self.claude_binaries

    def install(self) -> None:
        if self._saved is not None:
            return
        guard, connect, connect_ex = self, socket.socket.connect, socket.socket.connect_ex

        def guarded_connect(sock: socket.socket, address: Any) -> None:
            if guard.refuses(address):
                raise ConnectionRefusedError(errno.ECONNREFUSED, f"tests never reach a real OpenBao (port {address[1]})")
            connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
            return errno.ECONNREFUSED if guard.refuses(address) else connect_ex(sock, address)

        from aeos.worker_runtime import processes

        popen_init = subprocess.Popen.__init__
        sandbox_create = processes.SandboxDirs.__dict__["create"]

        def guarded_popen_init(popen: subprocess.Popen[Any], args: Any, *rest: Any, **kw: Any) -> None:
            executable = kw.get("executable", rest[1] if len(rest) > 1 else None)
            if guard.refuses_exec(args, executable, kw.get("env"), bool(kw.get("shell"))):
                raise FileNotFoundError(errno.ENOENT, "tests never run the owner's real Claude Code (its login is in "
                                        "the OS credential store; opt in with the real_claude_cli fixture)")
            popen_init(popen, args, *rest, **kw)

        def guarded_sandbox_create(cls: type[processes.SandboxDirs], root: Path) -> processes.SandboxDirs:
            dirs = sandbox_create.__func__(cls, root)
            write_sandbox_keyring_config(dirs.home)
            return dirs

        self._saved = (keyring.get_keyring(), connect, connect_ex, popen_init, sandbox_create)
        keyring.set_keyring(self.keyring)
        socket.socket.connect = guarded_connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign,assignment]
        subprocess.Popen.__init__ = guarded_popen_init  # type: ignore[method-assign,assignment]
        processes.SandboxDirs.create = classmethod(guarded_sandbox_create)  # type: ignore[method-assign,assignment]
        _reset_secret_provider_cache()

    def uninstall(self) -> None:
        if self._saved is None:
            return
        from aeos.worker_runtime import processes

        previous, connect, connect_ex, popen_init, sandbox_create = self._saved
        self._saved = None
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        subprocess.Popen.__init__ = popen_init  # type: ignore[method-assign]
        processes.SandboxDirs.create = sandbox_create  # type: ignore[method-assign]
        keyring.set_keyring(previous)
        _reset_secret_provider_cache()


def _reset_secret_provider_cache() -> None:
    from aeos.secrets.provider import reset_provider_cache

    reset_provider_cache()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_secret_stores() -> Iterator[SecretGuard]:
    """The SecretGuard is active for the whole session, before any module- or test-scoped fixture runs."""
    guard = SecretGuard(real_vault_ports(), real_claude_binaries())
    guard.install()
    try:
        yield guard
    finally:
        guard.uninstall()


@pytest.fixture(autouse=True)
def _hermetic_secrets_per_test(_hermetic_secret_stores: SecretGuard) -> Iterator[HermeticKeyring]:
    """Each test starts with an empty keyring, no provider override and no cached vault client or token; a keyring
    backend the test installed is replaced again afterwards."""
    from aeos.secrets.provider import set_provider_override

    kr = _hermetic_secret_stores.keyring = HermeticKeyring()
    keyring.set_keyring(kr)
    set_provider_override(None)
    _reset_secret_provider_cache()
    yield kr
    keyring.set_keyring(kr)
    _reset_secret_provider_cache()


REAL_CLAUDE_OPT_IN = ("DLY_AEOS_REAL_CLAUDE", "DLY_AEOS_LIVE_CLAUDE")
REAL_SECRETS_OPT_IN = "DLY_AEOS_REAL_SECRETS"


@contextlib.contextmanager
def lifted_secret_guard(guard: SecretGuard, environ: Mapping[str, str]) -> Iterator[None]:
    """Owner opt-in: the whole SecretGuard is off inside the block (vault, OS keyring, sandbox keyring config and the
    refusal to start the owner's real Claude Code), so the test reads the owner's real vault and OS keyring; it is on
    again afterwards, also when the test fails. Skips unless ``DLY_AEOS_REAL_SECRETS=1``; the opt-in live provider test
    (tests/acceptance/test_delivery.py) reads its low-privilege token this way."""
    if environ.get(REAL_SECRETS_OPT_IN) != "1":
        pytest.skip(f"owner opt-in: {REAL_SECRETS_OPT_IN}=1 lets this test read the owner's real secret stores")
    guard.uninstall()
    try:
        yield
    finally:
        guard.install()


@pytest.fixture
def real_secret_stores(_hermetic_secret_stores: SecretGuard) -> Iterator[None]:
    with lifted_secret_guard(_hermetic_secret_stores, os.environ):
        yield


@pytest.fixture
def real_claude_cli(_hermetic_secret_stores: SecretGuard) -> Iterator[None]:
    """Owner opt-in: lets this test run the owner's real Claude Code, which reads its login from the OS credential
    store. Skips unless ``DLY_AEOS_REAL_CLAUDE=1`` (or ``DLY_AEOS_LIVE_CLAUDE=1``, which already spends a Max call)."""
    if not any(os.environ.get(name) == "1" for name in REAL_CLAUDE_OPT_IN):
        pytest.skip("owner opt-in: DLY_AEOS_REAL_CLAUDE=1 runs the owner's real Claude Code (its login is in the OS "
                    "credential store)")
    _hermetic_secret_stores.allow_real_claude = True
    try:
        yield
    finally:
        _hermetic_secret_stores.allow_real_claude = False


def make_app(settings: config_mod.Settings, include: list[str] | None = None):
    from aeos.api.app import create_app

    return create_app(settings, include=include, testing=True, init_db=False)


class AuthedClient:
    """TestClient wrapper that attaches the CSRF header automatically after login."""

    def __init__(self, client: TestClient):
        self.client = client
        self.csrf: str | None = None
        self.session: dict | None = None

    def login(self, username: str, password: str) -> dict:
        r = self.client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        self.session = r.json()
        self.csrf = self.session["csrf_token"]
        return self.session

    def _h(self, headers: dict | None) -> dict:
        h = dict(headers or {})
        if self.csrf:
            h.setdefault("X-CSRF-Token", self.csrf)
        return h

    def get(self, url: str, **kw):
        return self.client.get(url, **kw)

    def post(self, url: str, headers: dict | None = None, **kw):
        return self.client.post(url, headers=self._h(headers), **kw)

    def put(self, url: str, headers: dict | None = None, **kw):
        return self.client.put(url, headers=self._h(headers), **kw)

    def patch(self, url: str, headers: dict | None = None, **kw):
        return self.client.patch(url, headers=self._h(headers), **kw)

    def delete(self, url: str, headers: dict | None = None, **kw):
        return self.client.delete(url, headers=self._h(headers), **kw)

    def select_domain(self, domain: Domain = Domain.PERSONAL, project_id: str | None = None) -> dict:
        body: dict = {"domain": domain.value}
        if project_id:
            body["project_id"] = project_id
        r = self.post("/api/session/context", json=body)
        assert r.status_code == 200, r.text
        return r.json()


def create_admin(db: Session, username: str = "owner", password: str = ADMIN_PW):
    from aeos.identity import service as identity

    u = identity.bootstrap_admin(db, username=username, password=password)
    db.commit()
    return u


def create_viewer(db: Session, username: str = "viewer", password: str = VIEWER_PW):
    from aeos.identity import service as identity

    u = identity.create_user(db, username=username, password=password, role=Role.VIEWER)
    db.commit()
    return u


@pytest.fixture
def app(settings: config_mod.Settings):
    return make_app(settings)


@pytest.fixture
def client(app) -> Iterator[AuthedClient]:
    with TestClient(app, base_url="http://testserver") as c:
        yield AuthedClient(c)


@pytest.fixture
def admin_client(client: AuthedClient, db: Session) -> AuthedClient:
    create_admin(db)
    client.login("owner", ADMIN_PW)
    return client


@pytest.fixture
def personal_client(admin_client: AuthedClient) -> AuthedClient:
    admin_client.select_domain(Domain.PERSONAL)
    return admin_client


@pytest.fixture
def absent_modules(monkeypatch: pytest.MonkeyPatch):
    """Simulate that control-plane modules are not installed: importing them raises ImportError.

    Used by fail-closed tests now that every module exists in the integrated tree."""
    import sys

    def _absent(*names: str) -> None:
        for name in names:
            monkeypatch.setitem(sys.modules, name, None)

    return _absent


# ------------------------------------------------------------------ CI shards (ADR-0115 amendment 2026-10-26)
# ``pytest --shard K/N`` (or AEOS_TEST_SHARD=K/N) keeps only the test files of shard K of N. The files collected
# for the run are packed into N shards by their measured seconds (tests/ci_durations.json, rebuilt from CI JUnit
# XML by scripts/ci_durations.py) with greedy longest-processing-time: heaviest file first, each onto the lightest
# shard so far; equal weights are ordered by a SHA-256 of the path, so the split depends on nothing but the file
# list and the weights (not on the OS, the input order or PYTHONHASHSEED). Every collected file lands in exactly one
# shard: running shards 1..N with the same arguments runs every selected test exactly once. Without --shard nothing
# changes. The code lives in this file because the verifier's copy of the trusted suite carries only
# tests/__init__.py, tests/conftest.py and tests/acceptance (aeos.verification.suite).

SHARD_ENV = "AEOS_TEST_SHARD"
SHARD_WEIGHTS = Path(__file__).resolve().parent / "ci_durations.json"
_SHARD_RE = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")
_SHARD_SPEC = pytest.StashKey[tuple[int, int, dict[str, float]]]()
_SHARD_LINE = pytest.StashKey[str]()
_SHARD_INPUT = "aeos_shard"  # xdist workerinput/workeroutput key


def parse_shard(text: str) -> tuple[int, int]:
    """``"K/N"`` -> (K, N) with 1 <= K <= N."""
    m = _SHARD_RE.fullmatch(text or "")
    if not m:
        raise ValueError(f"expected K/N with 1 <= K <= N (for example 1/2), got {text!r}")
    k, n = int(m.group(1)), int(m.group(2))
    if not 1 <= k <= n:
        raise ValueError(f"expected K/N with 1 <= K <= N (for example 1/2), got {text!r}")
    return k, n


def load_shard_weights(path: Path = SHARD_WEIGHTS) -> dict[str, float]:
    """Seconds per test file (repository-relative, forward slashes) from the weights file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise ValueError(f"{path.name} has no 'files' mapping")
    weights: dict[str, float] = {}
    for rel, sec in files.items():
        if (not isinstance(rel, str) or not rel or isinstance(sec, bool) or not isinstance(sec, int | float)
                or not math.isfinite(sec) or sec < 0):
            raise ValueError(f"{path.name}: invalid weight {sec!r} for {rel!r}")
        weights[rel.replace("\\", "/")] = float(sec)
    return weights


def default_shard_weight(weights: Mapping[str, float]) -> float:
    """Weight of a file without a measured time (a new test file): the median measured file, 1.0 if none."""
    return float(statistics.median(weights.values())) if weights else 1.0


def assign_shards(files: Iterable[str], total: int, weights: Mapping[str, float],
                  default: float | None = None) -> list[list[str]]:
    """Pack *files* into *total* shards (greedy LPT). Returns the sorted file list of each shard (index 0 = shard 1)."""
    if total < 1:
        raise ValueError("at least one shard")
    fallback = default_shard_weight(weights) if default is None else default
    unique = sorted({f.replace("\\", "/") for f in files})

    def weight(rel: str) -> float:
        return weights.get(rel, fallback)

    order = sorted(unique, key=lambda rel: (-weight(rel), hashlib.sha256(rel.encode("utf-8")).hexdigest(), rel))
    loads = [0.0] * total
    shards: list[list[str]] = [[] for _ in range(total)]
    for rel in order:
        lightest = min(range(total), key=lambda i: (loads[i], i))
        shards[lightest].append(rel)
        loads[lightest] += weight(rel)
    return [sorted(s) for s in shards]


def _item_file(item: pytest.Item) -> str:
    """The test file of *item*, relative to the rootdir with forward slashes (the node id prefix)."""
    return item.nodeid.split("::", 1)[0]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--shard", action="store", default=None, metavar="K/N",
                     help="run only the test files of shard K of N, packed by measured seconds "
                          f"(tests/ci_durations.json); also read from {SHARD_ENV}")


def pytest_configure(config: pytest.Config) -> None:
    workerinput = getattr(config, "workerinput", None)  # pytest-xdist worker: the controller resolved the shard
    if isinstance(workerinput, dict) and workerinput.get(_SHARD_INPUT):
        requested = str(workerinput[_SHARD_INPUT])
    else:
        # read once and removed: pytest processes that tests start themselves are never sharded
        requested = config.getoption("shard", None) or os.environ.pop(SHARD_ENV, "")
    os.environ.pop(SHARD_ENV, None)
    if not requested.strip():
        return
    try:
        k, n = parse_shard(requested)
        weights = load_shard_weights()
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(f"--shard {requested}: {exc}") from exc
    config.stash[_SHARD_SPEC] = (k, n, weights)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """pytest-xdist controller: hand the resolved shard to every worker (the environment variable is gone)."""
    spec = node.config.stash.get(_SHARD_SPEC, None)
    if spec is not None:
        node.workerinput[_SHARD_INPUT] = f"{spec[0]}/{spec[1]}"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Runs after the -m/-k deselection: the shards split the files that still have selected tests."""
    spec = config.stash.get(_SHARD_SPEC, None)
    if spec is None:
        return
    k, n, weights = spec
    files = sorted({_item_file(i) for i in items})
    mine = set(assign_shards(files, n, weights)[k - 1])
    kept = [i for i in items if _item_file(i) in mine]
    dropped = [i for i in items if _item_file(i) not in mine]
    fallback = default_shard_weight(weights)
    unmeasured = sorted(f for f in mine if f not in weights)
    line = (f"AEOS test shard {k}/{n}: {len(mine)} of {len(files)} files, {len(kept)} of {len(items)} tests, "
            f"weight {sum(weights.get(f, fallback) for f in mine):.1f} of "
            f"{sum(weights.get(f, fallback) for f in files):.1f} s ({len(unmeasured)} files without a measured "
            f"weight counted as {fallback:.2f} s; tests/ci_durations.json)")
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
    config.stash[_SHARD_LINE] = line
    workeroutput = getattr(config, "workeroutput", None)
    if isinstance(workeroutput, dict):
        workeroutput[_SHARD_INPUT] = line


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """pytest-xdist controller: every worker computed the same shard; keep its summary line."""
    line = (getattr(node, "workeroutput", None) or {}).get(_SHARD_INPUT)
    if line:
        node.config.stash[_SHARD_LINE] = line


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    line = config.stash.get(_SHARD_LINE, None)
    if line:
        terminalreporter.write_line(line)
