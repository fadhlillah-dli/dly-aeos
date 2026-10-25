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


# the owner's decision on the S07 client restriction of the free Zen route (ADR-0041 amendment 2026-10-25)
CLIENT_TERMS = ("--accept-client-terms", "--client-terms-note", "read S07 client rule; broker proxying accepted")


def _activate(*extra: str, password: str, user: str = "owner", client_terms: bool = True):
    h = None
    from aeos.core.db import session_factory

    s = session_factory()()
    try:
        h = service.get_route(s, DS).config_hash
    finally:
        s.close()
    args = ["routes", "activate", DS, "--as", user, "--mode", "TRIAL", "--rationale", "pilot slice one",
            "--expected-hash", h, *(CLIENT_TERMS if client_terms else ()), *extra]
    return runner.invoke(app, args, input=password + "\n")


def test_activate_refuses_a_client_restricted_route_without_the_owners_decision(routes, monkeypatch):
    install_fakes(monkeypatch)
    create_admin(routes)
    r = _activate(password=ADMIN_PW, client_terms=False)
    assert r.exit_code == 1 and "CLIENT_TERMS_DECISION_REQUIRED" in r.output and r.output.isascii()
    routes.expire_all()
    assert routes.query(Decision).count() == 0 and service.get_route(routes, DS).status == "CANDIDATE"
    listed = runner.invoke(app, ["routes", "list", "--pool", "WORKER"])
    assert "client-terms=OWNER_DECISION_REQUIRED" in listed.output
    ok = _activate(password=ADMIN_PW)
    assert ok.exit_code == 0, ok.output
    routes.expire_all()
    d = routes.query(Decision).one()
    assert d.details["client_terms"]["decision"] == service.CLIENT_TERMS_ACCEPTED
    assert "client-terms=" not in runner.invoke(app, ["routes", "list", "--pool", "WORKER"]).output.split(DS)[1] \
        .splitlines()[0]


def test_set_endpoint_points_a_local_route_at_a_loopback_server(routes, monkeypatch):
    install_fakes(monkeypatch)
    create_admin(routes)
    key = "local/qwen3.8-flash-next"
    before = service.get_route(routes, key).config_hash
    base = ["routes", "set-endpoint", key, "--as", "owner", "--rationale", "llama.cpp server on this laptop"]
    bad = runner.invoke(app, [*base, "--endpoint", "http://192.168.1.5:8080/v1"], input=ADMIN_PW + "\n")
    assert bad.exit_code == 1 and "ENDPOINT_NOT_LOOPBACK" in bad.output
    wrong = runner.invoke(app, [*base, "--endpoint", "http://127.0.0.1:8080/v1"], input="nope\n")
    assert wrong.exit_code != 0 and "password check failed" in wrong.output
    ok = runner.invoke(app, [*base, "--endpoint", "http://127.0.0.1:8080/v1", "--model", "qwen3.8-flash-next-q4",
                             "--quantization", "Q4_K_M", "--hardware", "M3 Max 64GB"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert "endpoint=http://127.0.0.1:8080/v1" in ok.output and ok.output.isascii() and ADMIN_PW not in ok.output
    routes.expire_all()
    r = service.get_route(routes, key)
    assert r.endpoint == "http://127.0.0.1:8080/v1" and r.config_hash != before
    assert r.mode_params["upstream_model"] == "qwen3.8-flash-next-q4" and r.mode_params["quantization"] == "Q4_K_M"
    assert r.mode_params["hardware"] == "M3 Max 64GB" and r.mode_params["inference_backend"] == "UNKNOWN"
    assert routes.query(AuditEvent).filter_by(action="route.set_endpoint", target_id=key).count() == 1


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


def test_activate_without_decision_registry_fails_closed(routes, monkeypatch, absent_modules):
    absent_modules("aeos.policy.decisions")
    create_admin(routes)  # policy module not installed: nothing is recorded, route stays CANDIDATE
    r = _activate(password=ADMIN_PW)
    assert r.exit_code == 1 and "DECISION_REGISTRY_UNAVAILABLE" in r.output
    routes.expire_all()
    assert service.get_route(routes, DS).status == "CANDIDATE"


def test_activate_records_the_owners_reading_of_unknown_terms(routes, monkeypatch):
    """§23 "klaim provider bukan bukti": UNKNOWN training terms allow PUBLIC only until the owner records their
    own reading - possible from the CLI too (--inputs-used-for-training / --trial-only + --terms-note)."""
    from datetime import timedelta

    install_fakes(monkeypatch)
    create_admin(routes)
    key = "openrouter/poolside/laguna-s-2.1:free"
    r = service.get_route(routes, key)
    r.availability = "AVAILABLE"
    r.free_until = utcnow() + timedelta(days=30)  # independent of the real end of the free period
    r.config_hash = service.compute_config_hash(r)
    routes.commit()
    base = ["routes", "activate", key, "--as", "owner", "--mode", "TRIAL", "--rationale", "arena trial run",
            "--expected-hash", r.config_hash, "--data-class", "PUBLIC", "--data-class", "INTERNAL", "--terms-reviewed",
            "--terms-note", "OpenRouter provider page: prompts are not used for training"]
    refused = runner.invoke(app, base, input=ADMIN_PW + "\n")
    assert refused.exit_code == 1 and "UNKNOWN" in refused.output
    incomplete = runner.invoke(app, [*base, "--no-inputs-used-for-training"], input=ADMIN_PW + "\n")
    assert incomplete.exit_code == 1 and "TERMS_RECORD_INCOMPLETE" in incomplete.output
    ok = runner.invoke(app, [*base, "--no-inputs-used-for-training", "--no-trial-only"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    routes.expire_all()
    route = service.get_route(routes, key)
    assert route.status == "TRIAL" and route.allowed_data_classes == ["PUBLIC", "INTERNAL"]
    assert route.data_terms["owner_terms_record"] is True and route.data_terms["may_use_inputs_for_training"] is False


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
