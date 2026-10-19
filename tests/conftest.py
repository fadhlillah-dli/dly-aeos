"""Shared test fixtures. Every test gets an isolated data directory and database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aeos.core import config as config_mod
from aeos.core.db import create_all, init_engine, session_factory
from aeos.core.enums import Domain, Role

ADMIN_PW = "correct-horse-battery-staple"
VIEWER_PW = "read-only-observer-pass-77"


@pytest.fixture(autouse=True)
def _controller_not_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI runners (GitHub-hosted Windows) run elevated; the product refuses the process sandbox for an elevated
    controller (§25 non-root). That refusal is tested explicitly (test_worker_runtime_sandbox); every other test
    runs as a normal user would."""
    from aeos.worker_runtime import sandbox

    monkeypatch.setattr(sandbox, "_elevation_probe", lambda: False)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[config_mod.Settings]:
    monkeypatch.setenv("AEOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "no-config.yaml"))
    for k in list(os.environ):
        if k.startswith("AEOS_") and k not in {"AEOS_DATA_DIR", "AEOS_CONFIG"}:
            monkeypatch.delenv(k, raising=False)
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
