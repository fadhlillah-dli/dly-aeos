"""Local authentication service (§26).

* Server-side opaque sessions — the cookie carries a random token, the DB stores only its hash.
* Argon2id password hashes; login throttling per username and per client.
* Session revocation; idle and absolute expiry.
* CSRF token bound to the session (double-submit via header).
* No default credentials: the first SUPER_ADMIN is created by a local CLI action; recovery issues
  a one-time temporary password and revokes every session for that user.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings
from aeos.core.enums import Domain, Role
from aeos.core.errors import Conflict, Forbidden, TooManyRequests, Unauthenticated, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.identity.passwords import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from aeos.models.identity import AuthSession, LoginAttempt, SystemSetting, User

BOOTSTRAP_KEY = "bootstrap.admin_created"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Principal:
    user: User
    session: AuthSession
    role: Role
    allowed_domains: list[Domain] = field(default_factory=list)
    allowed_project_ids: list[str] | None = None  # None = not restricted by user scope

    @property
    def is_admin(self) -> bool:
        return self.role == Role.SUPER_ADMIN

    @property
    def active_domain(self) -> Domain | None:
        d = self.session.selected_domain
        return Domain(d) if d else None

    def can_access_domain(self, domain: str | Domain) -> bool:
        return Domain(domain) in self.allowed_domains

    def can_access_project(self, project_id: str, domain: str | Domain) -> bool:
        if not self.can_access_domain(domain):
            return False
        return self.allowed_project_ids is None or project_id in self.allowed_project_ids


# ---------------------------------------------------------------- bootstrap & users


def admin_exists(session: Session) -> bool:
    return bool(
        session.scalar(
            select(func.count()).select_from(User).where(User.role == Role.SUPER_ADMIN.value, User.is_active)
        )
    )


def bootstrap_complete(session: Session) -> bool:
    row = session.get(SystemSetting, BOOTSTRAP_KEY)
    return bool(row and row.value) and admin_exists(session)


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: Role,
    display_name: str = "",
    actor: str = "cli",
    must_change_password: bool = False,
) -> User:
    username = username.strip().lower()
    if not username or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValidationFailed("username must be alphanumeric (._- allowed)")
    if session.scalar(select(User).where(User.username == username)):
        raise Conflict("username already exists")
    validate_password_policy(password, username=username)
    user = User(
        username=username,
        display_name=display_name or username,
        password_hash=hash_password(password),
        role=role.value,
        must_change_password=must_change_password,
        password_changed_at=utcnow(),
    )
    session.add(user)
    session.flush()
    audit.record(
        session, actor_type="cli" if actor == "cli" else "user", actor_id=actor, action="identity.user.create",
        target_type="user", target_id=user.id, details={"username": username, "role": role.value},
    )
    return user


def bootstrap_admin(session: Session, *, username: str, password: str, display_name: str = "") -> User:
    """Create the first SUPER_ADMIN. Only allowed while no active admin exists (local CLI only)."""
    if admin_exists(session):
        raise Conflict("a SUPER_ADMIN already exists; use `aeos admin recover` for recovery")
    user = create_user(session, username=username, password=password, role=Role.SUPER_ADMIN,
                       display_name=display_name, actor="cli")
    row = session.get(SystemSetting, BOOTSTRAP_KEY)
    if row is None:
        session.add(SystemSetting(key=BOOTSTRAP_KEY, value=True))
    else:
        row.value = True
    audit.record(session, actor_type="cli", actor_id="local-operator", action="identity.bootstrap.complete",
                 target_type="user", target_id=user.id)
    return user


def recover_user(session: Session, *, username: str) -> str:
    """Local, authorised recovery: one-time temporary password + revoke all sessions (§26)."""
    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        raise ValidationFailed("unknown user")
    temp = secrets.token_urlsafe(18)
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    user.is_active = True
    user.password_changed_at = utcnow()
    revoke_all_sessions(session, user.id, reason="recovery")
    audit.record(session, actor_type="cli", actor_id="local-operator", action="identity.user.recover",
                 target_type="user", target_id=user.id)
    return temp


def change_password(session: Session, principal: Principal, *, current: str, new: str) -> None:
    user = principal.user
    if not verify_password(user.password_hash, current):
        audit.record(session, actor_type="user", actor_id=user.id, action="identity.password.change",
                     result="DENIED", reason="current password mismatch")
        raise Forbidden("current password is incorrect")
    if current == new:
        raise ValidationFailed("new password must differ from the current one")
    validate_password_policy(new, username=user.username)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    # Revoke other sessions; keep the current one.
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.id != principal.session.id,
               AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason="password-changed")
    )
    audit.record(session, actor_type="user", actor_id=user.id, action="identity.password.change")


# ---------------------------------------------------------------- throttling


def _recent_failures(session: Session, *, username: str, client_ip: str, window: timedelta) -> tuple[int, int]:
    since = utcnow() - window
    by_user = session.scalar(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.username == username, LoginAttempt.success.is_(False), LoginAttempt.at >= since
        )
    ) or 0
    by_ip = session.scalar(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.client_ip == client_ip, LoginAttempt.success.is_(False), LoginAttempt.at >= since
        )
    ) or 0
    return int(by_user), int(by_ip)


def _last_success(session: Session, username: str):
    return session.scalar(
        select(func.max(LoginAttempt.at)).where(LoginAttempt.username == username, LoginAttempt.success.is_(True))
    )


# ---------------------------------------------------------------- sessions


def login(
    session: Session, settings: Settings, *, username: str, password: str, client_ip: str, user_agent: str
) -> tuple[str, AuthSession, User]:
    username = (username or "").strip().lower()
    window = timedelta(seconds=settings.login_lockout_seconds)
    by_user, by_ip = _recent_failures(session, username=username, client_ip=client_ip, window=window)
    last_ok = ensure_aware(_last_success(session, username))
    if last_ok and last_ok >= utcnow() - window:
        # failures before the last success do not count toward the lockout
        by_user = int(session.scalar(
            select(func.count()).select_from(LoginAttempt).where(
                LoginAttempt.username == username, LoginAttempt.success.is_(False), LoginAttempt.at > last_ok
            )
        ) or 0)
    if by_user >= settings.login_max_failures or by_ip >= settings.login_max_failures * 4:
        audit.record(session, actor_type="user", actor_id=username, action="identity.login", result="DENIED",
                     reason="throttled", details={"client_ip": client_ip})
        session.commit()
        raise TooManyRequests("too many failed attempts; try again later", code="LOGIN_THROTTLED")

    user = session.scalar(select(User).where(User.username == username))
    ok = verify_password(user.password_hash if user else DUMMY_HASH, password or "")
    if not user or not ok or not user.is_active:
        session.add(LoginAttempt(username=username, client_ip=client_ip, success=False))
        audit.record(session, actor_type="user", actor_id=username, action="identity.login", result="DENIED",
                     reason="invalid credentials", details={"client_ip": client_ip})
        session.commit()  # persist the failed attempt even though we raise
        raise Unauthenticated("invalid username or password", code="INVALID_CREDENTIALS")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    session.add(LoginAttempt(username=username, client_ip=client_ip, success=True))
    token = secrets.token_urlsafe(32)
    now = utcnow()
    auth = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        user_agent=(user_agent or "")[:256],
        client_ip=client_ip[:64],
    )
    session.add(auth)
    user.last_login_at = now
    session.flush()
    audit.record(session, actor_type="user", actor_id=user.id, action="identity.login",
                 target_type="session", target_id=auth.id)
    return token, auth, user


def resolve_session(session: Session, settings: Settings, token: str | None) -> Principal:
    if not token:
        raise Unauthenticated("not signed in")
    auth = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    now = utcnow()
    if auth is None or auth.revoked_at is not None:
        raise Unauthenticated("session is not valid")
    if ensure_aware(auth.expires_at) <= now:
        raise Unauthenticated("session expired", code="SESSION_EXPIRED")
    if ensure_aware(auth.last_seen_at) + timedelta(minutes=settings.session_idle_minutes) <= now:
        auth.revoked_at = now
        auth.revoked_reason = "idle-timeout"
        session.commit()
        raise Unauthenticated("session expired due to inactivity", code="SESSION_EXPIRED")
    user = session.get(User, auth.user_id)
    if user is None or not user.is_active:
        raise Unauthenticated("account disabled")
    if (now - ensure_aware(auth.last_seen_at)).total_seconds() > 30:
        auth.last_seen_at = now
    role = Role(user.role)
    domains = [Domain(d) for d in (user.domain_scope or [d.value for d in Domain])]
    return Principal(user=user, session=auth, role=role, allowed_domains=domains,
                     allowed_project_ids=user.project_scope)


def logout(session: Session, principal: Principal) -> None:
    principal.session.revoked_at = utcnow()
    principal.session.revoked_reason = "logout"
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="identity.logout",
                 target_type="session", target_id=principal.session.id)


def revoke_all_sessions(session: Session, user_id: str, *, reason: str) -> int:
    res = session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    return int(getattr(res, "rowcount", 0) or 0)


def verify_csrf(principal: Principal, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(principal.session.csrf_token, header_value or "")


def select_domain(session: Session, principal: Principal, domain: Domain) -> None:
    """§05 step 2 / U-05: explicit human domain choice, recorded and audited."""
    if not principal.can_access_domain(domain):
        audit.record(session, actor_type="user", actor_id=principal.user.id, action="session.domain.select",
                     result="DENIED", domain=domain.value, reason="domain not in user scope")
        raise Forbidden("domain is outside your scope")
    previous = principal.session.selected_domain
    principal.session.selected_domain = domain.value
    if previous != domain.value:
        principal.session.selected_project_id = None
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="session.domain.select",
                 domain=domain.value, details={"previous": previous})
