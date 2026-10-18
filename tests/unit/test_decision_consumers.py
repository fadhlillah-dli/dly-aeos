"""Write-enablement and exception decisions recorded through the decisions API/CLI with exactly the target
shapes their consumers read (§19, §20, §23, §26; ADR-0031/0042/0051 amendments 2026-10-18).

Each test records the decision through the API, proves the consumer's behaviour changes, revokes it and
proves the behaviour reverts (fail closed)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision
from aeos.policy.facade import WriteIntent, check_external_write
from tests.conftest import ADMIN_PW, AuthedClient
from tests.unit.test_planning_support import make_project

EXPIRES = (utcnow() + timedelta(days=30)).isoformat()
BASE = {"auth_scope": {"scope": "owner decision for this host"}, "rationale": "keputusan pemilik yang dicatat"}


def _post(c: AuthedClient, body: dict, code: int = 201) -> dict:
    r = c.post("/api/decisions", json={**BASE, **body})
    assert r.status_code == code, r.text
    return r.json()


def _revoke(c: AuthedClient, decision_id: str) -> None:
    r = c.post(f"/api/decisions/{decision_id}/revoke", json={"reason": "dicabut pemilik"})
    assert r.status_code == 200, r.text


def _codes(res) -> set[str]:
    return {r.split(":", 1)[0] for r in res.reasons}


def _check(db, intent) -> set[str]:
    """Run the interlock as a consumer and commit its audit row (releases SQLite's write lock for the API)."""
    db.expire_all()
    codes = _codes(check_external_write(db, intent))
    db.commit()
    return codes


def test_shapes_endpoint_lists_the_consumer_target_shapes(personal_client):
    shapes = personal_client.get("/api/decisions/shapes").json()
    assert shapes["write_enablement"]["providers"] == ["google-drive", "github-issues", "github-repo"]
    ex = shapes["exceptions"]
    assert (ex["secret_store_interim_keyring"]["target_type"], ex["secret_store_interim_keyring"]["target_id"]) == (
        "secret_store", "interim-keyring")
    assert ex["worker_sandbox_process"]["flags"] == ["allow_bash"] and ex["worker_sandbox_process"]["available"]
    assert shapes["office_authorization"]["available"] is False  # only in OFFICE sessions


def test_write_enablement_toggles_the_interlock(personal_client, db):
    project = make_project(db)
    owner_id = personal_client.session["user"]["id"]
    intent = WriteIntent(actor_user_id=owner_id, project_id=project.id, domain="PERSONAL", provider="google-drive",
                         operation="create_doc", target={"provider": "google-drive", "root_id": "root"},
                         approved_plan_version_id=None, approved_hash=None, decision_id=None,
                         capability_expires_at=utcnow() + timedelta(minutes=5))
    assert "WRITE_DISABLED" in _check(db, intent)
    d = _post(personal_client, {"kind": "WRITE_ENABLEMENT", "provider": "google-drive", "enabled": True})
    assert (d["target_type"], d["target_id"], d["details"]["enabled"]) == ("provider_write", "google-drive", True)
    db.expire_all()
    codes = _check(db, intent)
    assert not codes & {"WRITE_DISABLED", "WRITE_ENABLEMENT_DECISION_MISSING"}
    _revoke(personal_client, d["id"])
    db.expire_all()
    assert "WRITE_DISABLED" in _check(db, intent)  # revoke turns the flag off


def test_interim_keyring_exception_switches_the_secret_store_and_revoke_reverts(personal_client, db, settings,
                                                                                monkeypatch):
    from aeos.core.errors import Blocked
    from aeos.secrets import provider as sp

    class NoBao:
        def health(self):
            return {"status": "UNREACHABLE"}

        def ensure_usable(self):
            raise Blocked("OpenBao not reachable (test)", code="SECRET_STORE_UNAVAILABLE")

    monkeypatch.setattr(sp, "openbao_provider", lambda s: NoBao())
    monkeypatch.setattr(sp, "keyring_backend", lambda: (True, "test.FakeKeyring"))
    assert sp.interim_exception(settings) is None and sp.select_backend(settings).backend == "none"
    d = _post(personal_client, {"kind": "EXCEPTION", "exception": "secret_store_interim_keyring",
                                "expires_at": EXPIRES})
    assert (d["target_type"], d["target_id"]) == ("secret_store", "interim-keyring")
    assert d["details"]["secret_store_interim_keyring"] is True and d["domain"] == "PERSONAL"
    info = sp.interim_exception(settings)
    assert info is not None and info["decision_id"] == d["id"]
    assert sp.select_backend(settings).backend == "os-keyring-interim"
    _revoke(personal_client, d["id"])
    assert sp.interim_exception(settings) is None and sp.select_backend(settings).backend == "none"


def test_free_form_exception_on_the_interim_target_gets_the_same_shape(personal_client, settings):
    from aeos.secrets import provider as sp

    d = _post(personal_client, {"kind": "EXCEPTION", "target_type": "secret_store", "target_id": "interim-keyring",
                                "details": {"secret_store_interim_keyring": True}, "expires_at": EXPIRES})
    assert d["details"]["exception"] == "secret_store_interim_keyring"
    assert sp.interim_exception(settings)["decision_id"] == d["id"]
    bad = personal_client.post("/api/decisions", json={**BASE, "kind": "EXCEPTION", "target_type": "secret_store",
                                                       "target_id": "interim-keyring", "expires_at": EXPIRES,
                                                       "details": {"secret_store_interim_keyring": "yes"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "EXCEPTION_DETAILS_INVALID"


@pytest.mark.parametrize("body,code", [
    ({"target_type": "repository", "target_id": "o/r", "details": {"allow_process_sandbox": True}},
     "EXCEPTION_TARGET_MISMATCH"),  # a consumer-read flag on a target nobody reads it from
    ({"exception": "worker_sandbox_process", "target_type": "worker_sandbox", "target_id": "container"},
     "EXCEPTION_TARGET_MISMATCH"),
    ({"exception": "secret_store_interim_keyring", "details": {"allow_bash": True}}, "EXCEPTION_TARGET_MISMATCH"),
])
def test_exception_shapes_are_exact(personal_client, body, code):
    r = personal_client.post("/api/decisions", json={**BASE, "kind": "EXCEPTION", "expires_at": EXPIRES, **body})
    assert r.status_code == 422 and r.json()["error"]["code"] == code


def test_office_never_gets_interim_keyring_or_process_sandbox(admin_client):
    admin_client.select_domain(Domain.OFFICE)
    for preset in ("secret_store_interim_keyring", "worker_sandbox_process"):
        r = admin_client.post("/api/decisions", json={**BASE, "kind": "EXCEPTION", "exception": preset,
                                                      "expires_at": EXPIRES})
        assert r.status_code == 422 and r.json()["error"]["code"] == "EXCEPTION_DOMAIN_DENIED"


def test_process_sandbox_exception_makes_the_sandbox_ready_and_revoke_blocks(personal_client, db, monkeypatch):
    from aeos.worker_runtime import sandbox

    caps = {"checked_at": utcnow().isoformat(), "levels": {
        "process": {"available": True, "strength": "WEAK"},
        "container": {"available": False, "strength": "NOT_PROVEN", "evidence": "docker CLI not found (test)"},
        "vm": {"available": False}}, "best_available": "process"}
    monkeypatch.setattr(sandbox, "sandbox_capabilities", lambda refresh=False: dict(caps))
    assert sandbox.sandbox_readiness(db, domain="PERSONAL")["status"] == "BLOCKED"
    d = _post(personal_client, {"kind": "EXCEPTION", "exception": "worker_sandbox_process", "allow_bash": True,
                                "expires_at": EXPIRES})
    assert d["details"] == {**d["details"], "allow_process_sandbox": True, "allow_bash": True}
    db.expire_all()
    ready = sandbox.sandbox_readiness(db, domain="PERSONAL")
    assert ready["status"] == "READY" and ready["kind"] == "process" and ready["decision_id"] == d["id"]
    assert sandbox.process_shell_allowed(db, ready["decision_id"]) is True
    assert sandbox.sandbox_readiness(db, domain="OFFICE")["status"] == "BLOCKED"  # never for OFFICE
    # a renewal without bash supersedes the old one: the shell is no longer allowed
    d2 = _post(personal_client, {"kind": "EXCEPTION", "exception": "worker_sandbox_process", "expires_at": EXPIRES})
    db.expire_all()
    ready = sandbox.sandbox_readiness(db, domain="PERSONAL")
    assert ready["decision_id"] == d2["id"] and sandbox.process_shell_allowed(db, d2["id"]) is False
    assert db.get(Decision, d["id"]).status == "SUPERSEDED"
    _revoke(personal_client, d2["id"])
    db.expire_all()
    assert sandbox.sandbox_readiness(db, domain="PERSONAL")["status"] == "BLOCKED"


def test_office_company_authorisation_opens_office_writes_and_revoke_closes(admin_client, db):
    admin_client.select_domain(Domain.OFFICE)
    project = make_project(db, domain=Domain.OFFICE)
    owner_id = admin_client.session["user"]["id"]
    intent = WriteIntent(actor_user_id=owner_id, project_id=project.id, domain="OFFICE", provider="confluence",
                         operation="create_doc", target={"provider": "confluence", "root_id": "123"},
                         approved_plan_version_id=None, approved_hash=None, decision_id=None,
                         capability_expires_at=utcnow() + timedelta(minutes=5))
    assert "OFFICE_NOT_AUTHORISED" in _check(db, intent)
    d = _post(admin_client, {"kind": "POLICY", "office_authorization": True, "company_reference": "Memo IT 2026/10",
                             "allowed_providers": ["confluence", "jira"],
                             "office_policy": {"atlassian_deployment": "CLOUD"}})
    assert (d["target_type"], d["target_id"]) == ("office_policy", "OFFICE")
    db.expire_all()
    codes = _check(db, intent)
    assert not codes & {"OFFICE_NOT_AUTHORISED", "OFFICE_PROVIDER_NOT_AUTHORISED", "ATLASSIAN_DEPLOYMENT_UNKNOWN"}
    _revoke(admin_client, d["id"])
    db.expire_all()
    assert "OFFICE_NOT_AUTHORISED" in _check(db, intent)


def test_cli_records_and_revokes_with_the_admin_password(settings, db):
    from aeos.cli import app
    from aeos.secrets import provider as sp
    from tests.conftest import create_admin

    create_admin(db)
    runner = CliRunner()
    wrong = runner.invoke(app, ["policy", "decide", "--kind", "EXCEPTION", "--as", "owner", "--domain", "PERSONAL",
                                "--exception", "secret_store_interim_keyring", "--days", "7",
                                "--rationale", "OpenBao belum terpasang di laptop ini"], input="not-it\n")
    assert wrong.exit_code != 0 and sp.interim_exception(settings) is None
    ok = runner.invoke(app, ["policy", "decide", "--kind", "EXCEPTION", "--as", "owner", "--domain", "PERSONAL",
                             "--exception", "secret_store_interim_keyring", "--days", "7",
                             "--rationale", "OpenBao belum terpasang di laptop ini"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "secret_store:interim-keyring" in ok.output
    info = sp.interim_exception(settings)
    assert info is not None
    gone = runner.invoke(app, ["policy", "revoke", info["decision_id"], "--as", "owner", "--reason", "OpenBao siap"],
                         input=ADMIN_PW + "\n")
    assert gone.exit_code == 0, gone.output
    assert sp.interim_exception(settings) is None
    enable = runner.invoke(app, ["policy", "decide", "--kind", "WRITE_ENABLEMENT", "--as", "owner", "--domain",
                                 "PERSONAL", "--provider", "github-issues", "--enable",
                                 "--rationale", "publikasi issue diizinkan"], input=ADMIN_PW + "\n")
    assert enable.exit_code == 0, enable.output
    from aeos.policy import enablement

    db.expire_all()
    assert enablement.flag(db, "github-issues") is True
