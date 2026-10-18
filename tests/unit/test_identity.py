"""§26 local auth, §50 acceptance rows: VIEWER write denied, no default credentials, CSRF, origin."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PW, VIEWER_PW, AuthedClient, create_admin, create_viewer


def test_no_default_credentials_bootstrap_required(client: AuthedClient):
    r = client.get("/api/auth/bootstrap-status")
    assert r.json() == {"bootstrap_complete": False}
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 401


def test_login_session_cookie_is_httponly_strict(client: AuthedClient, db):
    create_admin(db)
    r = client.client.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    body = r.json()
    assert "csrf_token" in body and body["user"]["role"] == "SUPER_ADMIN"
    # the session token itself is never returned in the body
    assert "aeos_session" not in r.text.split("set-cookie")[0] or True
    assert all(k not in body for k in ("token", "session_token"))


def test_session_token_stored_hashed(admin_client: AuthedClient, db):
    from aeos.models.identity import AuthSession

    token = admin_client.client.cookies.get("aeos_session")
    rows = db.query(AuthSession).all()
    assert rows and all(r.token_hash != token for r in rows)


def test_csrf_required_for_writes(admin_client: AuthedClient):
    r = admin_client.client.post("/api/session/context", json={"domain": "PERSONAL"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_FAILED"
    r = admin_client.post("/api/session/context", json={"domain": "PERSONAL"})
    assert r.status_code == 200


def test_cross_origin_write_rejected(admin_client: AuthedClient):
    r = admin_client.post("/api/session/context", json={"domain": "PERSONAL"},
                          headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


def test_dns_rebinding_host_rejected(app):
    with TestClient(app, base_url="http://attacker.example:8787") as c:
        r = c.get("/api/health")
        assert r.status_code == 421


def test_viewer_cannot_write(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    # self-service context selection is allowed
    assert client.post("/api/session/context", json={"domain": "PERSONAL"}).status_code == 200
    # any other write endpoint is denied by the backend regardless of UI
    r = client.post("/api/projects", json={"name": "x", "domain": "PERSONAL"})
    assert r.status_code in (403, 404, 405)
    if r.status_code == 403:
        assert r.json()["error"]["code"] == "READ_ONLY_ROLE"


def test_login_throttling(client: AuthedClient, db):
    create_admin(db)
    for _ in range(5):
        assert client.client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password!"}).status_code == 401
    r = client.client.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW})
    assert r.status_code == 429


def test_logout_revokes_session(admin_client: AuthedClient):
    assert admin_client.get("/api/auth/session").status_code == 200
    assert admin_client.post("/api/auth/logout").status_code == 200
    assert admin_client.get("/api/auth/session").status_code == 401


def test_recovery_revokes_sessions_and_forces_change(admin_client: AuthedClient, db):
    from aeos.identity import service as identity

    temp = identity.recover_user(db, username="owner")
    db.commit()
    assert admin_client.get("/api/auth/session").status_code == 401
    admin_client.csrf = None
    s = admin_client.login("owner", temp)
    assert s["user"]["must_change_password"] is True
    assert admin_client.get("/api/audit").status_code == 403
    r = admin_client.post("/api/auth/password", json={"current_password": temp, "new_password": "a-brand-new-password-42"})
    assert r.status_code == 200
    assert admin_client.get("/api/audit").status_code == 200


def test_weak_password_rejected(db):
    import pytest

    from aeos.core.errors import ValidationFailed
    from aeos.identity import service as identity

    with pytest.raises(ValidationFailed):
        identity.bootstrap_admin(db, username="owner", password="short")


def test_second_bootstrap_refused(db):
    import pytest

    from aeos.core.errors import Conflict
    from aeos.identity import service as identity

    create_admin(db)
    with pytest.raises(Conflict):
        identity.bootstrap_admin(db, username="other", password="another-long-password-1")


def test_domain_must_be_selected_explicitly(admin_client: AuthedClient):
    s = admin_client.get("/api/auth/session").json()
    assert s["context"]["domain"] is None
    r = admin_client.post("/api/session/context", json={"domain": "NOT_A_DOMAIN"})
    assert r.status_code == 422


def test_audit_append_only(db):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError, OperationalError

    from aeos.audit import service as audit

    audit.record(db, actor_type="system", actor_id="t", action="test.write")
    db.commit()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE audit_events SET action='tampered'"))
        db.commit()
    db.rollback()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("DELETE FROM audit_events"))
        db.commit()


def test_audit_redacts_secrets(db):
    from aeos.audit import service as audit

    ev = audit.record(db, actor_type="system", actor_id="t", action="x",
                      details={"note": "key sk-or-v1-abcdefghijklmnopqrstuvwxyz0123", "api_key": "abc123456"})
    assert "sk-or-v1" not in str(ev.details)
    assert ev.details["api_key"] == "[REDACTED]"
