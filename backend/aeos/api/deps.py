"""Shared FastAPI dependencies: DB session, principal, RBAC, CSRF, domain context.

Write endpoints commit explicitly (``db.commit()``) so a failed commit is reported to the client
instead of being swallowed after the response was sent.

Enforcement lives here, in the backend (§35 "menyembunyikan tombol di UI bukan enforcement"):
* every unsafe method needs a valid session + matching CSRF header;
* VIEWER is read-only for every write endpoint (§02, §50);
* users with ``must_change_password`` can only reach the password/logout endpoints;
* domain-scoped endpoints require an explicit, human-selected domain (U-05).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from aeos.core.config import Settings, get_settings
from aeos.core.db import session_factory
from aeos.core.enums import Domain, Role
from aeos.core.errors import Blocked, Forbidden, NotFound
from aeos.identity import service as identity
from aeos.identity.service import Principal
from aeos.models.portfolio import Project

SESSION_COOKIE = "aeos_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Writes a VIEWER (or a must-change-password user) may still perform on their own session.
SELF_SERVICE_PATHS = frozenset({"/api/auth/logout", "/api/auth/password", "/api/session/context"})


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        if db.in_transaction():
            db.rollback()
        db.close()


DB = Annotated[Session, Depends(get_db)]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_principal(request: Request, db: DB, settings: SettingsDep) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    principal = identity.resolve_session(db, settings, token)
    method = request.method.upper()
    path = request.url.path
    if method not in SAFE_METHODS:
        if not identity.verify_csrf(principal, request.headers.get(CSRF_HEADER)):
            raise Forbidden("missing or invalid CSRF token", code="CSRF_FAILED")
        if principal.role == Role.VIEWER and path not in SELF_SERVICE_PATHS:
            raise Forbidden("VIEWER role is read-only", code="READ_ONLY_ROLE")
    if principal.user.must_change_password and path not in SELF_SERVICE_PATHS and path != "/api/auth/session":
        raise Forbidden("password change required", code="PASSWORD_CHANGE_REQUIRED")
    db.commit()  # persist last_seen_at
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_admin(principal: CurrentPrincipal) -> Principal:
    if principal.role != Role.SUPER_ADMIN:
        raise Forbidden("SUPER_ADMIN required", code="ADMIN_REQUIRED")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def require_domain(principal: CurrentPrincipal) -> Domain:
    """U-05/§05: the backend refuses domain-scoped requests without a valid human choice."""
    domain = principal.active_domain
    if domain is None:
        raise Blocked("select PERSONAL or OFFICE first", code="DOMAIN_NOT_SELECTED")
    if not principal.can_access_domain(domain):
        raise Forbidden("selected domain is outside your scope")
    return domain


ActiveDomain = Annotated[Domain, Depends(require_domain)]


def load_project(db: Session, principal: Principal, project_id: str, *, domain: Domain | None = None) -> Project:
    """Load a project the principal may see in the active domain. Cross-domain access is a 404 so
    that Office metadata does not leak into a Personal session (§35, §50)."""
    project = db.get(Project, project_id)
    active = domain or principal.active_domain
    if project is None or active is None or project.domain != active.value:
        raise NotFound("project not found")
    if not principal.can_access_project(project.id, project.domain):
        raise NotFound("project not found")
    return project
