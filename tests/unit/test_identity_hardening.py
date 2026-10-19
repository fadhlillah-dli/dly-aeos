"""Local auth hardening (master §26 auth lokal; ADR-0004): Argon2id parameters, audited auth events,
single-use time-limited recovery credential."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.identity import User
from tests.conftest import ADMIN_PW, AuthedClient, create_admin


def test_password_hash_is_argon2id_with_pinned_parameters(db):
    from aeos.identity.passwords import hash_password, needs_rehash

    user = create_admin(db)
    assert user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert not needs_rehash(user.password_hash)
    # a weaker/other-type hash is upgraded at the next login
    from argon2 import PasswordHasher, Type

    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.I).hash("x" * 16)
    assert needs_rehash(weak) and hash_password("y" * 16).startswith("$argon2id$")


def _actions(db, prefix: str = "") -> list[tuple[str, str, str]]:
    rows = db.scalars(select(AuditEvent).where(AuditEvent.action.startswith(prefix)).order_by(AuditEvent.seq))
    return [(r.action, r.result, r.reason) for r in rows]


def test_authentication_events_are_audited(client: AuthedClient, db, settings):
    create_admin(db)
    assert client.client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password-1"}
                              ).status_code == 401
    assert ("identity.login", "DENIED", "invalid credentials") in _actions(db, "identity.login")
    client.login("owner", ADMIN_PW)
    assert ("identity.login", "SUCCESS", "") in _actions(db, "identity.login")
    client.select_domain()
    assert ("session.domain.select", "SUCCESS", "") in _actions(db, "session.domain.select")
    r = client.post("/api/auth/password", json={"current_password": "not-the-password", "new_password": "x" * 14})
    assert r.status_code == 403
    assert ("identity.password.change", "DENIED", "current password mismatch") in _actions(db, "identity.password")
    r = client.post("/api/auth/password", json={"current_password": ADMIN_PW, "new_password": "fresh-long-pass-9182"})
    assert r.status_code == 200
    assert ("identity.password.change", "SUCCESS", "") in _actions(db, "identity.password")
    assert client.post("/api/auth/logout").status_code == 200
    assert ("identity.logout", "SUCCESS", "") in _actions(db, "identity.logout")
    from aeos.identity import service as identity

    identity.recover_user(db, username="owner")
    db.commit()
    assert [a for a in _actions(db, "identity.user.recover")] == [("identity.user.recover", "SUCCESS", "")]
    for _ in range(settings.login_max_failures):
        client.client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password-1"})
    r = client.client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password-1"})
    assert r.status_code == 429
    assert ("identity.login", "DENIED", "throttled") in _actions(db, "identity.login")


def test_recovery_credential_is_single_use_and_expires(client: AuthedClient, db):
    from aeos.identity import service as identity

    create_admin(db)
    temp = identity.recover_user(db, username="owner")
    db.commit()
    user = db.scalar(select(User).where(User.username == "owner"))
    assert user.temp_password_expires_at is not None
    assert user.temp_password_expires_at <= utcnow() + identity.TEMP_PASSWORD_TTL
    first = client.login("owner", temp)
    assert first["user"]["must_change_password"] is True
    r = client.client.post("/api/auth/login", json={"username": "owner", "password": temp})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TEMP_PASSWORD_USED"
    # a new recovery issues a new credential; once expired it no longer works
    temp2 = identity.recover_user(db, username="owner")
    db.commit()
    db.refresh(user)
    user.temp_password_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    r = client.client.post("/api/auth/login", json={"username": "owner", "password": temp2})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TEMP_PASSWORD_EXPIRED"
    assert ("identity.login", "DENIED", "temporary password expired") in _actions(db, "identity.login")
    # a valid recovery used once, then a real password: normal logins again, credential cleared
    temp3 = identity.recover_user(db, username="owner")
    db.commit()
    client.login("owner", temp3)
    assert client.post("/api/auth/password", json={"current_password": temp3,
                                                   "new_password": "after-recovery-pass-77"}).status_code == 200
    db.expire_all()
    user = db.scalar(select(User).where(User.username == "owner"))
    assert user.temp_password_expires_at is None and user.temp_password_used_at is None
    client.login("owner", "after-recovery-pass-77")
    client.login("owner", "after-recovery-pass-77")
