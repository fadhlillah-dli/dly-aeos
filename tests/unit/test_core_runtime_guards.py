"""Local runtime guards (master §06, §09): data dir location, dev origins, dev credentials, IPv6 port check."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aeos.core import config as config_mod
from aeos.core.config import Settings
from aeos.core.paths import repo_root
from tests.conftest import ADMIN_PW, create_admin


def test_relative_data_dir_is_rejected():
    """§06: data paths come from OS/profile configuration, never from the cwd via a relative path."""
    with pytest.raises(ValueError, match="absolute"):
        Settings(data_dir=Path("data-rel"))


def test_data_dir_inside_the_repo_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="inside the source tree"):
        Settings(data_dir=repo_root() / "app-data")
    assert Settings(data_dir=tmp_path / "d").data_dir == tmp_path / "d"
    # the CLI never creates runtime state there either
    target = repo_root() / "app-data-guard-test"
    monkeypatch.setenv("AEOS_DATA_DIR", str(target))
    monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "none.yaml"))
    config_mod.reset_settings_cache()
    try:
        from aeos.cli import app

        result = CliRunner().invoke(app, ["db", "upgrade"])
        assert result.exit_code != 0
        assert not target.exists()
    finally:
        config_mod.reset_settings_cache()


def test_doctor_reports_data_dir_in_repo(settings, db):
    from aeos.core.timeutil import utcnow
    from aeos.doctor.checks.host import host_data_dir
    from aeos.doctor.model import CheckContext

    inside = Settings.model_construct(**{**settings.model_dump(), "data_dir": repo_root() / "app-data"})
    out = host_data_dir(CheckContext(settings=inside, session=db, runner=None, repo=repo_root(),  # type: ignore[arg-type]
                                     now=utcnow()))
    assert out.status == "FAILED" and out.error_code == "DATA_DIR_IN_REPO"
    ok = host_data_dir(CheckContext(settings=settings, session=db, runner=None, repo=repo_root(),  # type: ignore[arg-type]
                                    now=utcnow()))
    assert ok.error_code != "DATA_DIR_IN_REPO" and ok.evidence["separate_from_repo"] is True


@pytest.mark.parametrize("origin", ["https://evil.example", "http://192.168.1.5:5173", "http://127.0.0.1",
                                    "http://localhost:99999", "*"])
def test_extra_allowed_origins_accept_loopback_only(origin):
    with pytest.raises(ValueError, match="loopback"):
        Settings(extra_allowed_origins=[origin])
    ok = Settings(extra_allowed_origins=["http://127.0.0.1:3000", "http://[::1]:3000"])
    assert "http://127.0.0.1:3000" in ok.allowed_origins


@pytest.mark.parametrize("dev_mode", [False, True])
def test_dev_frontend_origin_only_in_dev_mode_and_csrf_still_required(settings, db, dev_mode):
    from aeos.api.app import create_app

    create_admin(db)
    s = settings.model_copy(update={"dev_mode": dev_mode})
    app = create_app(s, include=["aeos.identity.router", "aeos.api.system"], init_db=False)
    dev_origin = f"http://127.0.0.1:{s.dev_frontend_port}"
    with TestClient(app, base_url=f"http://127.0.0.1:{s.port}") as c:
        r = c.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW}, headers={"Origin": dev_origin})
        if not dev_mode:
            assert r.status_code == 403 and r.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
            r = c.get("/api/health", headers={"Host": f"127.0.0.1:{s.dev_frontend_port}"})
            assert r.status_code == 421
            return
        assert r.status_code == 200
        assert c.get("/api/health", headers={"Host": f"127.0.0.1:{s.dev_frontend_port}"}).status_code == 200
        # dev mode never relaxes CSRF
        r = c.post("/api/session/context", json={"domain": "PERSONAL"}, headers={"Origin": dev_origin})
        assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def _listener(family: int, host: str) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


def test_port_check_handles_ipv6_loopback():
    from aeos.cli import port_in_use

    assert Settings(host="::1").base_url == "http://[::1]:8787"
    with _listener(socket.AF_INET, "127.0.0.1") as sock:
        assert port_in_use("127.0.0.1", sock.getsockname()[1]) is True
    try:
        six = _listener(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this host")
    with six:
        port = six.getsockname()[1]
        assert port_in_use("::1", port) is True
    assert port_in_use("::1", port) is False  # closed: an explanation path, never a traceback


def test_serve_explains_ipv6_port_conflict(settings, monkeypatch):
    from aeos.cli import app

    try:
        six = _listener(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this host")
    monkeypatch.setenv("AEOS_HOST", "::1")
    with six:
        result = CliRunner().invoke(app, ["serve", "--port", str(six.getsockname()[1])])
    assert result.exit_code == 2 and "already in use" in result.output


def test_dev_server_refuses_real_credentials_profile(settings, monkeypatch):
    """§09: a hot-reloading server never runs with the owner's real data dir / keyring service."""
    import aeos.core.paths as paths
    from aeos.cli import app

    monkeypatch.setattr(paths, "default_data_dir", lambda: settings.data_dir)
    for flags in (["--dev"], ["--reload"]):
        result = CliRunner().invoke(app, ["serve", *flags, "--port", "1"])
        assert result.exit_code == 2 and "Refusing to start a development server" in result.output, result.output
        assert "default (real) data directory" in result.output and "keyring service" in result.output
    # a separate dev profile passes the guard (the busy port then stops it before anything binds)
    monkeypatch.setenv("AEOS_KEYRING_SERVICE", "dly-aeos-dev")
    monkeypatch.setattr(paths, "default_data_dir", lambda: settings.data_dir.parent / "real-profile")
    with _listener(socket.AF_INET, "127.0.0.1") as sock:
        result = CliRunner().invoke(app, ["serve", "--dev", "--port", str(sock.getsockname()[1])])
    assert result.exit_code == 2 and "already in use" in result.output
    assert "Refusing to start a development server" not in result.output


def test_serve_release_mode_refuses_without_a_verified_active_release(settings, monkeypatch):
    from aeos.cli import app

    with _listener(socket.AF_INET, "127.0.0.1") as sock:
        port = sock.getsockname()[1]
    result = CliRunner().invoke(app, ["serve", "--release", "active", "--port", str(port)])
    assert result.exit_code == 2 and "NO_ACTIVE_RELEASE" in result.output
    pointer = settings.layout.released_controller / "ACTIVE.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"active": {"release_id": "0.1.0-deadbeef0000", "commit": "x"}}), encoding="utf-8")
    result = CliRunner().invoke(app, ["serve", "--port", str(port)])  # auto -> active release required
    assert result.exit_code == 2 and "Refusing to start the controller" in result.output
