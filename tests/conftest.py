"""Shared test fixtures. Every test gets an isolated data directory and database.

Also the CI shard option ``--shard K/N`` (see the section at the end of this file)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aeos.core import config as config_mod
from aeos.core.db import create_all, init_engine, session_factory
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


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[config_mod.Settings]:
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
    engine = init_engine(s)
    create_all(engine)
    _stamp_head(engine)
    yield s
    engine.dispose()
    config_mod.reset_settings_cache()
    _reset_process_caches()


_HEAD: list[str] = []


def _stamp_head(engine) -> None:
    """Record the migration head like `aeos db upgrade` does for a create_all database (fast path)."""
    if not _HEAD:
        from alembic.script import ScriptDirectory

        from aeos.core.migrate import alembic_config

        _HEAD.append(ScriptDirectory.from_config(alembic_config()).get_current_head() or "")
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
