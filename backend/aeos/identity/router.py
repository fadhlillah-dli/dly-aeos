from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from aeos.api.deps import DB, SESSION_COOKIE, CurrentPrincipal, SettingsDep, client_ip
from aeos.core.enums import Domain
from aeos.identity import service as identity

router = APIRouter(tags=["identity"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class ContextIn(BaseModel):
    domain: Domain
    project_id: str | None = None


def _session_view(principal: identity.Principal) -> dict[str, Any]:
    u = principal.user
    return {
        "user": {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role,
            "must_change_password": u.must_change_password,
        },
        "csrf_token": principal.session.csrf_token,
        "expires_at": principal.session.expires_at.isoformat(),
        "allowed_domains": [d.value for d in principal.allowed_domains],
        "context": {
            "domain": principal.session.selected_domain,
            "project_id": principal.session.selected_project_id,
        },
    }


@router.get("/api/auth/bootstrap-status")
def bootstrap_status(db: DB) -> dict[str, Any]:
    """Unauthenticated, non-sensitive: tells the login page whether `aeos admin create` is needed."""
    return {"bootstrap_complete": identity.bootstrap_complete(db)}


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: DB, settings: SettingsDep) -> dict[str, Any]:
    token, _auth, _user = identity.login(
        db, settings, username=body.username, password=body.password,
        client_ip=client_ip(request), user_agent=request.headers.get("user-agent", ""),
    )
    db.commit()
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", secure=settings.secure_cookies,
        max_age=settings.session_ttl_hours * 3600, path="/",
    )
    principal = identity.resolve_session(db, settings, token)
    return _session_view(principal)


@router.post("/api/auth/logout")
def logout(principal: CurrentPrincipal, response: Response, db: DB) -> dict[str, str]:
    identity.logout(db, principal)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed-out"}


@router.get("/api/auth/session")
def session_info(principal: CurrentPrincipal) -> dict[str, Any]:
    return _session_view(principal)


@router.post("/api/auth/password")
def change_password(body: PasswordChangeIn, principal: CurrentPrincipal, db: DB) -> dict[str, str]:
    identity.change_password(db, principal, current=body.current_password, new=body.new_password)
    db.commit()
    return {"status": "password-changed"}


@router.post("/api/session/context")
def set_context(body: ContextIn, principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    identity.select_domain(db, principal, body.domain)
    if body.project_id is not None:
        from aeos.api.deps import load_project

        project = load_project(db, principal, body.project_id, domain=body.domain)
        principal.session.selected_project_id = project.id
    db.commit()
    return _session_view(principal)
