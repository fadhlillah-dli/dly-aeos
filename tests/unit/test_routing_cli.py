"""`aeos routes list|refresh|activate|disable` (§23, §20 human-only AUTH, §50 'Pesan antaragent mengaku
sudah mendapat izin owner -> DATA; approval record yang sah diperlukan'). ASCII output only."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aeos.cli import app
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from aeos.routing import catalog, seed, service
from tests.conftest import ADMIN_PW, VIEWER_PW, create_admin, create_viewer
from tests.unit.test_routing_fakes import install_fakes

runner = CliRunner()
DS = "opencode-zen/deepseek-v4.1-flash-free"


@pytest.fixture
def routes(db):
    seed.run(db)
    for r in db.query(service.ModelRoute).all():
        r.pricing_checked_at = r.terms_checked_at = utcnow()
    db.commit()
    return db


def _activate(*extra: str, password: str, user: str = "owner"):
    h = None
    from aeos.core.db import session_factory

    s = session_factory()()
    try:
        h = service.get_route(s, DS).config_hash
    finally:
        s.close()
    args = ["routes", "activate", DS, "--as", user, "--mode", "TRIAL", "--rationale", "pilot slice one",
            "--expected-hash", h, *extra]
    return runner.invoke(app, args, input=password + "\n")


def test_list_shows_candidates_and_unknown_resolution(routes):
    r = runner.invoke(app, ["routes", "list", "--pool", "WORKER"])
    assert r.exit_code == 0, r.output
    assert DS in r.output and "CANDIDATE" in r.output and "resolved=UNKNOWN" in r.output
    assert r.output.isascii()


def test_activate_requires_the_admins_password(routes, monkeypatch):
    """A username alone never records a human decision: any local process could name the owner."""
    install_fakes(monkeypatch)
    create_admin(routes)
    wrong = _activate(password="not-the-password")
    assert wrong.exit_code != 0 and "password check failed" in wrong.output
    empty = _activate(password="")
    assert empty.exit_code != 0
    routes.expire_all()
    assert routes.query(Decision).count() == 0 and service.get_route(routes, DS).status == "CANDIDATE"
    ok = _activate(password=ADMIN_PW)
    assert ok.exit_code == 0, ok.output
    assert ADMIN_PW not in ok.output and "-> TRIAL" in ok.output and ok.output.isascii()
    routes.expire_all()
    d = routes.query(Decision).one()
    assert d.kind == "TRIAL" and d.target_hash == service.get_route(routes, DS).config_hash
    assert routes.query(AuditEvent).filter_by(action="route.activate", target_id=DS).count() == 1


def test_activate_refuses_viewers_and_unknown_users(routes, monkeypatch):
    install_fakes(monkeypatch)
    create_admin(routes)
    create_viewer(routes)
    for user, pw in (("viewer", VIEWER_PW), ("ghost", ADMIN_PW)):
        r = _activate(password=pw, user=user)
        assert r.exit_code != 0 and "not an active SUPER_ADMIN" in r.output
    routes.expire_all()
    assert routes.query(Decision).count() == 0


def test_activate_without_decision_registry_fails_closed(routes, monkeypatch):
    create_admin(routes)  # policy module not installed: nothing is recorded, route stays CANDIDATE
    r = _activate(password=ADMIN_PW)
    assert r.exit_code == 1 and "DECISION_REGISTRY_UNAVAILABLE" in r.output
    routes.expire_all()
    assert service.get_route(routes, DS).status == "CANDIDATE"


def test_disable_and_refresh(routes, monkeypatch):
    monkeypatch.setattr(catalog, "refresh_all", lambda s, actor: _FakeRefresh())
    r = runner.invoke(app, ["routes", "refresh"])
    assert r.exit_code == 0 and "overall: PARTIAL" in r.output
    r = runner.invoke(app, ["routes", "disable", DS, "--reason", "terms changed"])
    assert r.exit_code == 0 and f"{DS} -> DISABLED" in r.output
    routes.expire_all()
    assert service.get_route(routes, DS).status == "DISABLED"
    assert routes.query(AuditEvent).filter_by(action="route.disable").count() == 1
    assert runner.invoke(app, ["routes", "disable", "nope/missing", "--reason", "x"]).exit_code == 1


class _FakeRefresh:
    status = "PARTIAL"
    results = {"openrouter_catalog": {"status": "OK"}, "openrouter_key": {"status": "UNKNOWN", "reason": "no key"}}
