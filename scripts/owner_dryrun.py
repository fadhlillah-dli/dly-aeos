"""Dry-run an owner script against a copy of the live database and show what it does to readiness (LESSONS L13).

An owner script lives outside the repository (for example ``~/Documents/dly-aeos-owner-scripts``) and exposes
``run(client, username, password, ask)``: it logs in through ``client`` (an httpx-style client of the Control Center
API) and asks the owner through ``ask(prompt) -> str``. Incident 2026-10-28: a script was dry-run against a copy of
the database with no secrets at all; on the real system it activated two WORKER routes whose credential was not
stored and CAN_RUN_WORKER fell from READY to BLOCKED. This harness mirrors which secrets exist and compares the
Doctor capabilities before and after, so that change shows up before the owner runs the script.

Usage::

    python scripts/owner_dryrun.py SCRIPT [--answer TEXT ...] [--user NAME] [--accept-degrade REASON]

What it does:

1. Copies the live database (the path of the real settings) with sqlite3's online backup from a read-only
   connection (``mode=ro``) into a new temporary data dir, and copies the real ``aeos.yaml`` beside it; the data dir
   of the dry run is the temporary one (``AEOS_DATA_DIR``). The live file is only read.
2. Reads which secret references are PRESENT with the real ``aeos secrets status`` in a child process (metadata
   only, never a value). Owner scripts run in the PERSONAL domain, so only PERSONAL references are mirrored: the
   status runs without ``--include-office`` and an ``office/*`` row is ignored. In the dry run every secret access
   goes to an in-memory mirror that reports exactly those references as present, with dummy values; ``office/*`` is
   NO_ACCESS there, as it is for the controller until a company authorisation. A real store that cannot be read
   (sealed or stopped OpenBao: backend none, or a PERSONAL reference UNKNOWN) fails the dry run before anything is
   copied: without the presence the result says nothing about the real system. The dry-run process itself has the null keyring
   backend, OpenBao at a closed loopback port, and refuses every TCP connect to a non-loopback address and to the
   loopback ports of the real OpenBao and the real Control Center, so no provider call and no live write can happen.
3. Creates a temporary SUPER_ADMIN in the copy and calls ``run(client, username, password, ask)`` with a TestClient
   of the real app; ``ask``, ``input()`` and ``getpass.getpass()`` all take the ``--answer`` values in order (a
   getpass answer is not echoed). A prompt with no answer left fails the run: ``no --answer left for prompt: P``.
4. Runs the Doctor (non-functional checks only: no Claude call, no quota, no network probe) before and after, and
   prints the capability table (before -> after) and every check whose status changed. A gating check (critical or
   required tier, a project check, a FAILED guard) of a capability that newly fails always counts as worse, also
   when the capability was already BLOCKED before (``CAN_RUN_WORKER: BLOCKED -> BLOCKED``) and when it got better
   at the same time because another check recovered (``BLOCKED -> DEGRADED``): the real system may be READY where
   the copy is not. Host checks observe the real machine as ``aeos doctor`` does: while the Doctor runs (never
   while the script runs) the loopback port of the real Control Center is open to it, so ``host.port`` sees the
   running server (CONNECTED through ``/api/health``) instead of a port the harness made unbindable.

Exit codes and the last line: 0 ``OWNER_DRYRUN: OK``; 1 ``OWNER_DRYRUN: DEGRADED`` when a capability got worse
(READY < DEGRADED < UNKNOWN < BLOCKED; NOT_APPLICABLE counts as READY) or one of its gating checks newly fails; 0 ``OWNER_DRYRUN: DEGRADED-ACCEPTED`` when
``--accept-degrade REASON`` was given (the reason is printed; never OK); 2 ``OWNER_DRYRUN: FAIL`` when the harness,
the script or the presence probe failed (an unavailable real secret store included). The temporary data dir is removed in every case. ASCII output.

Run it with the project interpreter (``.venv/bin/python``) from the checkout whose code the server runs: the copy
must be at that checkout's migration head, otherwise the run fails (the API of another version is no evidence).
``tests/unit/test_owner_dryrun.py`` holds the bite tests.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import errno
import importlib.util
import io
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

VERDICT = "OWNER_DRYRUN"
DEFAULT_USER = "dryrun-admin"
# READY < DEGRADED < UNKNOWN < BLOCKED: any step up is worse. NOT_APPLICABLE is not a loss of readiness.
RANK = {"READY": 0, "NOT_APPLICABLE": 0, "DEGRADED": 1, "UNKNOWN": 2, "BLOCKED": 3}
STATES = ("PRESENT", "MISSING", "EXPIRED", "UNKNOWN", "NO_ACCESS")
_ROW = re.compile(r"^(\S+)\s+(" + "|".join(STATES) + r")\s+backend=(\S+)\s+updated=(\S+)\s+expires=(\S+)\s*$")
_STORE = re.compile(r"^store:\s+(\S+)\s+backend=(\S+)\s*$")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}
ENV_KEYS = ("AEOS_DATA_DIR", "AEOS_CONFIG", "AEOS_OPENBAO_ADDR", "PYTHON_KEYRING_BACKEND")
NULL_KEYRING = "keyring.backends.null.Keyring"


class HarnessError(Exception):
    """The dry run could not be carried out; the message says why (exit 2)."""


class MissingAnswer(BaseException):
    """The script asked a question for which no --answer is left. A BaseException, so a script's own
    ``except Exception`` cannot hide it."""


# ------------------------------------------------------------------------------------------- output


def say(text: object = "") -> None:
    from aeos.inventory.asciitext import ascii_safe

    sys.stdout.write(ascii_safe(text) + "\n")
    sys.stdout.flush()


class _AsciiWriter(io.TextIOBase):
    """Passes the script's own output through as ASCII (legacy Windows consoles, AGENTS.md)."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        from aeos.inventory.asciitext import ascii_safe

        self._target.write(ascii_safe(s))
        return len(s)

    def flush(self) -> None:
        self._target.flush()


# ------------------------------------------------------------------------------------ secret presence


@dataclass
class Presence:
    """Which secret references exist in the real store: names and expiry only, never a value."""

    backend: str
    available: bool
    present: dict[str, datetime | None] = field(default_factory=dict)
    no_access: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)


def _when(text: str) -> datetime | None:
    if text in ("", "-"):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _office(ref: str) -> bool:
    return ref.split("/", 1)[0] == "office"


def parse_presence(text: str) -> Presence:
    """Parse the output of ``aeos secrets status`` (the PERSONAL view). ``office/*`` rows are ignored even when
    present: owner scripts run in the PERSONAL domain. A store that is unavailable (backend none, or a PERSONAL
    reference UNKNOWN) is returned as unavailable with no references; ``dry_run`` refuses to run on it."""
    store = None
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        m = _STORE.match(line.strip())
        if m and store is None:
            store = m.group(2)
            continue
        r = _ROW.match(line)
        if r and not _office(r.group(1)):
            rows.append((r.group(1), r.group(2), r.group(5)))
    if store is None:
        raise HarnessError("could not read 'aeos secrets status' (no 'store:' line); nothing to mirror")
    unknown = any(state == "UNKNOWN" for _ref, state, _exp in rows)
    out = Presence(backend=store, available=store != "none" and not unknown)
    if not out.available:
        return out
    for ref, state, expires in rows:
        if state in ("PRESENT", "EXPIRED"):
            out.present[ref] = _when(expires)
        elif state == "NO_ACCESS":
            out.no_access.add(ref)
        else:
            out.missing.add(ref)
    return out


def probe_presence() -> Presence:
    """Run the real ``aeos secrets status`` (the metadata-only status path, PERSONAL view: no ``--include-office``)
    in a child process with the owner's environment; this process never opens the real keyring or OpenBao itself."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT / "backend"), env.get("PYTHONPATH", "")) if p)
    argv = [sys.executable, "-m", "aeos", "secrets", "status"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"'aeos secrets status' did not run ({type(exc).__name__})") from exc
    if proc.returncode != 0:
        from aeos.core.redaction import redact_text

        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise HarnessError(f"'aeos secrets status' exited {proc.returncode}: {redact_text(tail[0])[:200]}")
    return parse_presence(proc.stdout)


class PresenceMirror:
    """Secret provider of the dry run: the real store's PERSONAL references, dummy values, nothing else. ``office/*``
    is not mirrored: it is NO_ACCESS, as for the controller until a recorded company authorisation (§23)."""

    name = "dry-run-mirror"
    denied_namespaces = ("office",)

    def __init__(self, presence: Presence) -> None:
        self._present = {r: e for r, e in presence.present.items() if not _office(r)}
        self._no_access = set(presence.no_access)

    def _status(self, ref: str) -> Any:
        from aeos.core.errors import PolicyDenied
        from aeos.secrets.facade import SecretStatus, validate_ref

        validate_ref(ref)
        if _office(ref):
            raise PolicyDenied(f"secret {ref}: office references are not mirrored in a PERSONAL dry run",
                               code="SECRET_NO_ACCESS")
        if ref in self._no_access:
            raise PolicyDenied(f"secret {ref}: controller policy denies this path (mirrored)", code="SECRET_NO_ACCESS")
        exists = ref in self._present
        return SecretStatus(ref=ref, exists=exists, backend=self.name, expires_at=self._present.get(ref),
                            note="dry run: presence mirrored from the real store, value is a dummy")

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        from aeos.core.errors import Blocked

        if not self._status(ref).exists:
            raise Blocked(f"secret {ref} is not configured", code="SECRET_MISSING")
        return "dry-run-dummy-" + uuid.uuid5(uuid.NAMESPACE_URL, ref).hex

    def put(self, ref: str, value: str, *, actor: str) -> Any:
        self._status(ref)
        self._present[ref] = None  # the value itself is dropped
        return self._status(ref)

    def status(self, ref: str) -> Any:
        return self._status(ref)

    def list_status(self) -> list[Any]:
        return [self._status(r) for r in sorted(self._present)]

    def delete(self, ref: str, *, actor: str) -> None:
        self._present.pop(ref, None)


# -------------------------------------------------------------------------------------- the script


def has_run(path: Path) -> bool:
    """A top-level ``def run`` that takes at least four positional arguments (read with ast, never executed)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            a = node.args
            return a.vararg is not None or len(a.posonlyargs) + len(a.args) >= 4
    return False


def load_script(path: Path) -> Callable[..., Any]:
    name = f"aeos_owner_script_{re.sub(r'[^A-Za-z0-9_]', '_', path.stem)}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HarnessError(f"cannot load {path.name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    run = getattr(mod, "run", None)
    if not callable(run):
        raise HarnessError(f"{path.name} has no run(client, username, password, ask)")
    return run  # type: ignore[no-any-return]


class Answers:
    """The --answer queue. ``ask(prompt)``, ``input(prompt)`` and ``getpass.getpass(prompt)`` all take the next value;
    a getpass answer is not echoed."""

    def __init__(self, values: Sequence[str]) -> None:
        self.values = list(values)
        self.used = 0
        self.missing: str | None = None

    def _take(self, prompt: object, *, hidden: bool) -> str:
        text = str(prompt).strip()
        if self.used >= len(self.values):
            self.missing = text
            raise MissingAnswer(text)
        value = self.values[self.used]
        self.used += 1
        say(f"{text} [--answer {self.used}: {'(hidden, getpass)' if hidden else value}]")
        return value

    def __call__(self, prompt: object = "") -> str:
        return self._take(prompt, hidden=False)

    def getpass(self, prompt: object = "Password: ", stream: Any = None) -> str:
        return self._take(prompt, hidden=True)


# getpass.getpass is bound per platform at import (unix_getpass, win_getpass, fallback_getpass); route them all
_GETPASS_NAMES = ("getpass", "unix_getpass", "win_getpass", "fallback_getpass")


@contextlib.contextmanager
def prompts_routed(answers: Answers) -> Iterator[None]:
    """``input()`` and ``getpass.getpass()`` take the --answer queue; ``sys.stdin`` reads give EOF.

    getpass does not read sys.stdin: it opens /dev/tty (POSIX) or reads the console (Windows), so on a real terminal
    an unrouted getpass would block the dry run waiting for the owner. Applied while the script is imported (a
    module-level ``from getpass import getpass`` then binds the routed function) and while ``run()`` runs."""
    import getpass as getpass_mod

    saved_stdin, saved_input = sys.stdin, builtins.input
    saved_getpass = {n: getattr(getpass_mod, n) for n in _GETPASS_NAMES if hasattr(getpass_mod, n)}
    sys.stdin = io.StringIO("")
    builtins.input = answers
    for n in saved_getpass:
        setattr(getpass_mod, n, answers.getpass)
    try:
        yield
    finally:
        for n, fn in saved_getpass.items():
            setattr(getpass_mod, n, fn)
        sys.stdin, builtins.input = saved_stdin, saved_input


# ---------------------------------------------------------------------------------- isolation


def _port(addr: str, default: int) -> int:
    try:
        return urlsplit(addr).port or default
    except ValueError:
        return default


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _ip(host: object) -> Any:
    import ipaddress

    text = str(host).strip("[]").split("%", 1)[0].lower()
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_loopback(host: object) -> bool:
    if str(host).strip("[]").lower() in _LOOPBACK_NAMES:
        return True
    ip = _ip(host)
    return bool(ip is not None and ip.is_loopback)


@contextlib.contextmanager
def network_refused(denied_loopback_ports: set[int]) -> Iterator[None]:
    """Every TCP connect to a non-loopback address, and to the denied loopback ports (real OpenBao, the real Control
    Center), raises ConnectionRefusedError; a name that is not loopback does not resolve. Loopback stays open for
    the process's own plumbing (asyncio's socketpair on Windows)."""
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refused(address: Any) -> bool:
        if not isinstance(address, tuple) or len(address) < 2:
            return False  # AF_UNIX and friends
        if not _is_loopback(address[0]):
            return True
        try:
            return int(address[1]) in denied_loopback_ports
        except (TypeError, ValueError):
            return True

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if refused(address):
            raise ConnectionRefusedError(errno.ECONNREFUSED,
                                         f"owner dry run: network refused ({address[0]}:{address[1]})")
        connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        return errno.ECONNREFUSED if refused(address) else connect_ex(sock, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and _ip(name) is None and not _is_loopback(name):  # an address literal needs no DNS
            raise socket.gaierror(socket.EAI_NONAME, "owner dry run: name resolution refused")
        return getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = getaddrinfo


@contextlib.contextmanager
def doctor_probe_window(denied_ports: set[int], port: int) -> Iterator[None]:
    """Opens the real Control Center's loopback port while the Doctor runs, and only then (never during the script).

    ``host.port`` observes the machine, not the database: with the real server running, ``aeos doctor`` reports it
    CONNECTED through ``GET /api/health``. With the port refused the probe saw a port it could not bind and reported
    FAILED PORT_BIND_DENIED, a BLOCKED CAN_OPEN_DASHBOARD that does not exist. The non-functional Doctor only reads
    that port (a TCP connect and the health GET); the script cannot reach it, because it does not run meanwhile."""
    was_denied = port in denied_ports
    denied_ports.discard(port)
    try:
        yield
    finally:
        if was_denied:
            denied_ports.add(port)


@contextlib.contextmanager
def isolated(data_dir: Path, config: Path, presence: Presence, denied_ports: set[int], bao_port: int) -> Iterator[Any]:
    """The dry-run process state: temp data dir and config, null keyring, closed OpenBao, refused network, the
    presence mirror as secret provider and a database engine on the copy. Everything is restored afterwards.
    ``denied_ports`` is the guard's live set (the closed OpenBao port is added to it): ``doctor_probe_window``
    opens one port of it while only the Doctor runs."""
    import keyring
    from keyring.backends import null

    import aeos.core.db as db_mod
    from aeos.claude_bridge.binary import clear_probe_cache
    from aeos.core import config as config_mod
    from aeos.secrets import provider as provider_mod

    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    saved_keyring = keyring.get_keyring()
    saved_override = provider_mod._override
    saved_engine, saved_factory = db_mod._engine, db_mod._session_factory
    engine = None
    os.environ.update({"AEOS_DATA_DIR": str(data_dir), "AEOS_CONFIG": str(config),
                       "AEOS_OPENBAO_ADDR": f"http://127.0.0.1:{bao_port}", "PYTHON_KEYRING_BACKEND": NULL_KEYRING})
    keyring.set_keyring(null.Keyring())
    try:
        denied_ports.add(bao_port)
        with network_refused(denied_ports):
            config_mod.reset_settings_cache()
            provider_mod.reset_provider_cache()
            clear_probe_cache()
            provider_mod.set_provider_override(PresenceMirror(presence))
            settings = config_mod.get_settings()
            if settings.data_dir.resolve() != data_dir.resolve():
                raise HarnessError("the dry-run settings do not point at the temporary data dir")
            db_mod._engine, db_mod._session_factory = None, None  # never dispose the caller's engine
            engine = db_mod.init_engine(settings)
            yield settings
    finally:
        if engine is not None:
            engine.dispose()
        db_mod._engine, db_mod._session_factory = saved_engine, saved_factory
        provider_mod.set_provider_override(saved_override)
        provider_mod.reset_provider_cache()
        keyring.set_keyring(saved_keyring)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config_mod.reset_settings_cache()
        clear_probe_cache()


# ------------------------------------------------------------------------------------- the steps


def _backup(uri: str, dst: Path) -> None:
    source = sqlite3.connect(uri, uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def backup_live(src: Path, dst: Path) -> None:
    """sqlite3 online backup from a read-only connection: the live file is read, never written, locked for writing
    or migrated. With no ``-wal`` beside it (no writer has the database open) the read is also ``immutable``, so
    SQLite does not leave empty ``-wal``/``-shm`` files next to the live database; when a writer appears during
    that read, the copy is taken again through the normal read-only path. The copy must pass ``quick_check``."""
    wal = src.with_name(src.name + "-wal")
    base = src.resolve().as_uri() + "?mode=ro"
    idle = not wal.exists()
    _backup(base + ("&immutable=1" if idle else ""), dst)
    if idle and wal.exists():
        dst.unlink()
        _backup(base, dst)
    check = sqlite3.connect(str(dst))
    try:
        verdict = check.execute("PRAGMA quick_check").fetchone()
    finally:
        check.close()
    if not verdict or verdict[0] != "ok":
        raise HarnessError(f"the copy of the live database failed quick_check ({verdict})")


def check_schema() -> None:
    from aeos.core.migrate import migration_status

    st = migration_status()
    if st["current"] != st["head"]:
        raise HarnessError(f"the live database is at migration {st['current']} but this checkout's head is "
                           f"{st['head']}: run the dry run from the checkout the server runs")


def create_dryrun_admin(username: str, password: str) -> None:
    from aeos.core.db import session_scope
    from aeos.core.enums import Role
    from aeos.core.errors import AeosError
    from aeos.identity import service as identity

    try:
        with session_scope() as s:
            identity.create_user(s, username=username, password=password, role=Role.SUPER_ADMIN, actor="cli",
                                 display_name="owner dry run (temporary)")
    except AeosError as exc:
        raise HarnessError(f"cannot create the temporary admin '{username}' in the copy ({exc.code}: {exc.message}); "
                           "choose another --user") from exc


@dataclass
class Snapshot:
    capabilities: dict[str, tuple[str, str]]  # capability -> (status, first reason)
    checks: dict[str, tuple[str, str]]  # check id (+ project) -> (status, summary)
    gating: dict[str, dict[str, str]] = field(default_factory=dict)  # capability -> failing gating check -> status


def gating_failures(entries: Sequence[dict[str, Any]]) -> dict[str, str]:
    """The checks of one capability that hold it back (capabilities.evaluate): a not-ok check of the critical,
    required or project tier, or a guard check that FAILED. Advisory checks and guard notes never gate."""
    out: dict[str, str] = {}
    for c in entries:
        if c.get("ok"):
            continue
        tier, eff = c.get("tier"), str(c.get("effective_status") or "UNKNOWN")
        if tier in ("critical", "required", "project") or (tier == "guard" and eff == "FAILED"):
            out[str(c.get("check_id"))] = eff
    return out


def doctor_snapshot(settings: Any) -> Snapshot:
    """The Doctor as `aeos doctor` runs it without --functional, on the copy; the matrix from the stored results."""
    from aeos.core.db import session_factory
    from aeos.doctor.capabilities import capability_matrix
    from aeos.doctor.runner import run_doctor

    s = session_factory()()
    try:
        report = run_doctor(settings, s, functional=False, actor="owner-dryrun", trigger="cli")
        s.commit()
        matrix = capability_matrix(s, domain=None)
    finally:
        s.close()
    checks: dict[str, tuple[str, str]] = {}
    for r in report.results:
        pid = (r.scope or {}).get("project_id")
        checks[r.check_id + (f" @project {str(pid)[:8]}" if pid else "")] = (r.status, r.summary or "")
    caps = {c["capability"]: (c["status"], (c["reasons"] or [""])[0]) for c in matrix}
    gating = {c["capability"]: gating_failures(c.get("checks") or []) for c in matrix}
    return Snapshot(caps, checks, gating)


def worse(before: str, after: str) -> bool:
    return RANK.get(after, 2) > RANK.get(before, 2)


def newly_failing(before: Snapshot, after: Snapshot, cap: str) -> dict[str, str]:
    """Gating checks of ``cap`` that fail after the script and did not fail before."""
    was = before.gating.get(cap, {})
    return {cid: st for cid, st in after.gating.get(cap, {}).items() if cid not in was}


def print_diff(before: Snapshot, after: Snapshot) -> list[str]:
    """Print the capability table and the changed checks; return the capabilities that got worse: a worse status,
    or any gating check that newly fails, whatever the status did. An unchanged status (BLOCKED -> BLOCKED) and a
    better one (BLOCKED -> DEGRADED, because another check recovered) both can hide a regression."""
    from aeos.inventory.asciitext import table

    rows, degraded = [], []
    regressed: dict[str, dict[str, str]] = {}
    for cap in list(dict.fromkeys([*before.capabilities, *after.capabilities])):
        b = before.capabilities.get(cap, ("UNKNOWN", ""))[0]
        a, reason = after.capabilities.get(cap, ("UNKNOWN", ""))
        change = "WORSE" if worse(b, a) else ("better" if worse(a, b) else ("changed" if a != b else ""))
        fresh = newly_failing(before, after, cap)
        if fresh:
            regressed[cap] = fresh
            if change != "WORSE":
                change, reason = "WORSE", "newly failing: " + ", ".join(sorted(fresh))
        if change == "WORSE":
            degraded.append(cap)
        rows.append([cap, b, a, change, reason if a != b or cap in regressed else ""])
    say("")
    say("Capabilities (before -> after), Doctor without --functional on the copy:")
    say(table(["CAPABILITY", "BEFORE", "AFTER", "CHANGE", "REASON AFTER"], rows, max_width=60))
    changed = [(k, before.checks.get(k, ("-", ""))[0], *after.checks[k]) for k in after.checks
               if before.checks.get(k, ("-", ""))[0] != after.checks[k][0]]
    changed += [(k, before.checks[k][0], "-", "not run after") for k in before.checks if k not in after.checks]
    say("")
    if changed:
        say("Checks whose status changed:")
        for key, b, a, summary in changed:
            say(f"  {key}: {b} -> {a}  {summary[:160]}")
    else:
        say("Checks whose status changed: none")
    for cap in degraded:
        line = f"{cap}: {before.capabilities.get(cap, ('UNKNOWN', ''))[0]} -> {after.capabilities[cap][0]}"
        if cap in regressed:
            line += " (gating check newly failing: " + ", ".join(
                f"{cid} {before.checks.get(cid, ('-', ''))[0]} -> {st}" for cid, st in sorted(regressed[cap].items())) + ")"
        say(line)
    return degraded


def run_script(run: Callable[..., Any], settings: Any, username: str, password: str, answers: Answers) -> None:
    from fastapi.testclient import TestClient

    from aeos.api.app import create_app

    say("--- script output ---")
    saved_stdout = sys.stdout
    sys.stdout = _AsciiWriter(saved_stdout)  # type: ignore[assignment]
    try:
        with prompts_routed(answers), TestClient(create_app(settings, testing=True, init_db=False),
                                                 base_url="http://testserver") as client:
            result = run(client, username, password, answers)
    finally:
        sys.stdout = saved_stdout
    say("--- end of script output ---")
    if answers.missing is not None:  # the script caught MissingAnswer itself
        raise MissingAnswer(answers.missing)
    if result is not None:
        say(f"run() returned: {str(result)[:300]}")


def dry_run(script: Path, *, answers: Sequence[str], user: str, accept_degrade: str | None) -> int:
    from aeos.core.config import _config_file_path, get_settings
    from aeos.doctor.cli import data_dir_label

    if not script.is_file():
        raise HarnessError(f"no such script: {script}")
    if not has_run(script):
        raise HarnessError(f"{script.name} has no top-level run(client, username, password, ask); owner scripts "
                           "expose run(...) so they can be dry-run")
    live = get_settings()
    live_db = live.layout.db_path
    if not live_db.is_file():
        raise HarnessError(f"no live database at {data_dir_label(live)}")
    bao_live, cc_port = _port(live.openbao_addr, 8200), int(live.port)
    denied = {bao_live, cc_port}
    say(f"script: {script.name}")
    say(f"live data dir: {data_dir_label(live)} (read only; copied with sqlite3 backup, mode=ro)")
    presence = probe_presence()
    if not presence.available:
        # Without the presence the copy cannot show what the script does on the real system (a WORKER route whose
        # credential exists there is BLOCKED here before and after, so nothing would look worse).
        raise HarnessError(f"the real secret store is unavailable (backend {presence.backend}): the dry run cannot "
                           "see which credentials exist, so its verdict would say nothing about the real system. "
                           "Start or unseal OpenBao ('aeos vault status --verify'), then run the dry run again")
    say(f"secrets: {len(presence.present)} present, {len(presence.missing)} missing, {len(presence.no_access)} "
        f"no access in the real store (backend {presence.backend}; names only, no value read)")
    say("  present: " + (", ".join(sorted(presence.present)) or "none"))
    tmp = Path(tempfile.mkdtemp(prefix="aeos-owner-dryrun-"))
    try:
        data_dir, config = tmp / "data", tmp / "aeos.yaml"
        data_dir.mkdir()
        backup_live(live_db, data_dir / live_db.name)
        live_config = _config_file_path()
        if live_config.is_file():
            shutil.copy2(live_config, config)  # AEOS_DATA_DIR (set below) overrides its data_dir
        bao_port = _closed_loopback_port()
        with isolated(data_dir, config, presence, denied, bao_port) as settings:
            say("dry run: temporary data dir, null keyring, OpenBao at a closed loopback port, network refused")
            check_schema()
            ask = Answers(answers)
            with prompts_routed(ask):
                run = load_script(script)
            password = "Dry-" + uuid.uuid4().hex + "-Run!"
            create_dryrun_admin(user, password)
            say(f"temporary SUPER_ADMIN in the copy: {user}")
            # host.port as `aeos doctor` sees it (the running server CONNECTED), unless that port is OpenBao's too
            window = cc_port not in (bao_live, bao_port)
            say(f"Doctor before (non-functional checks; loopback port {cc_port} of the real Control Center is "
                + ("open to the Doctor, as in 'aeos doctor', never to the script) ..." if window
                   else "refused: it is OpenBao's too, so host.port may differ from 'aeos doctor') ..."))
            with doctor_probe_window(denied, cc_port) if window else contextlib.nullcontext():
                before = doctor_snapshot(settings)
            run_script(run, settings, user, password, ask)
            if ask.used < len(ask.values):
                say(f"WARNING: {len(ask.values) - ask.used} --answer value(s) were not asked for")
            say("Doctor after ...")
            with doctor_probe_window(denied, cc_port) if window else contextlib.nullcontext():
                after = doctor_snapshot(settings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if tmp.exists():
            say(f"WARNING: the temporary data dir could not be removed; delete it yourself: {tmp}")
    if tmp.exists():
        raise HarnessError("the temporary data dir (a copy of the live database) could not be removed")
    degraded = print_diff(before, after)
    if not degraded:
        if accept_degrade:
            say("note: --accept-degrade was not needed (no capability got worse)")
        say(f"{VERDICT}: OK")
        return 0
    if accept_degrade:
        say(f"degradation accepted by the owner: {accept_degrade}")
        say(f"{VERDICT}: DEGRADED-ACCEPTED")
        return 0
    say(f"{len(degraded)} capability(ies) got worse: {', '.join(degraded)}. Do not give this script to the owner as "
        "is; fix it, or state the reason with --accept-degrade.")
    say(f"{VERDICT}: DEGRADED")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dry-run an owner script against a copy of the live database and show "
                                 "the Doctor capability diff (LESSONS L13).")
    ap.add_argument("script", help="the owner script; it must define run(client, username, password, ask)")
    ap.add_argument("--answer", action="append", default=[], metavar="TEXT",
                    help="answer to the script's next ask() prompt, in order (repeat for each prompt)")
    ap.add_argument("--user", default=DEFAULT_USER, metavar="NAME",
                    help=f"name of the temporary SUPER_ADMIN created in the copy (default {DEFAULT_USER})")
    ap.add_argument("--accept-degrade", default=None, metavar="REASON",
                    help="accept a capability that gets worse, with the reason (exit 0, verdict DEGRADED-ACCEPTED)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.accept_degrade is not None and not args.accept_degrade.strip():
        say("--accept-degrade needs a reason")
        say(f"{VERDICT}: FAIL")
        return 2
    try:
        return dry_run(Path(args.script).expanduser(), answers=args.answer, user=args.user.strip().lower(),
                       accept_degrade=args.accept_degrade.strip() if args.accept_degrade else None)
    except MissingAnswer as exc:
        say(f"no --answer left for prompt: {exc}")
        say("give one --answer per prompt (ask, input or getpass), in order")
    except HarnessError as exc:
        say(f"harness failure: {exc}")
    except Exception as exc:
        from aeos.core.redaction import redact_text

        say("the dry run failed:")
        say(redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-3000:])
    except SystemExit as exc:
        say(f"the script called sys.exit({exc.code})")
    say(f"{VERDICT}: FAIL")
    return 2


if __name__ == "__main__":
    sys.exit(main())
