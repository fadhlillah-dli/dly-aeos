"""ClaudeNativeBridge API (§12, §35 Models & Routes / Integrations & Access)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from aeos.api.deps import DB, AdminPrincipal, CurrentPrincipal, SettingsDep
from aeos.audit import service as audit
from aeos.claude_bridge import bridge
from aeos.identity.service import Principal
from aeos.models.claude_bridge_ext import ClaudeCapabilityTest

router = APIRouter(tags=["claude_bridge"])


def mask_account(auth: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """The Max account e-mail is the owner's own: shown to SUPER_ADMIN, masked for other roles."""
    if principal.is_admin or not auth.get("email"):
        return auth
    return {**auth, "email": "(disembunyikan untuk peran ini)"}


@router.get("/api/claude/status")
def claude_status(db: DB, settings: SettingsDep, principal: CurrentPrincipal, refresh: bool = False) -> dict[str, Any]:
    """Binary/version/auth summary, env hygiene, policy models, last capability test, quota (UNKNOWN)."""
    # a forced re-probe spawns the official binary: SUPER_ADMIN only (otherwise the 60 s cache)
    out = bridge.status(db, settings, refresh=refresh and principal.is_admin)
    db.commit()  # persists a STALE mark when the binary changed since the last test
    out["auth"] = mask_account(out["auth"], principal)
    return out


class CapabilityTestBody(BaseModel):
    model: Literal["opus", "fable"] = "opus"


@router.post("/api/claude/capability-test")
def claude_capability_test(body: CapabilityTestBody, db: DB, settings: SettingsDep,
                           principal: AdminPrincipal) -> dict[str, Any]:
    """Run the real capability test (one tiny structured call on the owner's Max login)."""
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="claude.capability_test.requested",
                 target_type="component", target_id="claude-code", details={"model": body.model})
    db.commit()
    result = bridge.capability_test(body.model, actor=principal.user.username, settings=settings)
    audit.record(
        db, actor_type="user", actor_id=principal.user.id, action="claude.capability_test",
        result="SUCCESS" if result["status"] in {"VERIFIED", "DEGRADED"} else "FAILED",
        target_type="component", target_id="claude-code",
        details={"status": result["status"], "test_id": result.get("id"), "model_resolved": result.get("model_resolved"),
                 "error_code": result.get("error_code")},
    )
    db.commit()
    return result


@router.get("/api/claude/capability-tests")
def claude_capability_tests(db: DB, _p: CurrentPrincipal, limit: int = 20) -> dict[str, Any]:
    rows = list(db.scalars(select(ClaudeCapabilityTest).order_by(ClaudeCapabilityTest.created_at.desc())
                           .limit(max(1, min(limit, 100)))))
    return {
        "items": [bridge.capability_test_view(r, fingerprint=None, version=None) for r in rows],
        "source": "claude_capability_tests", "checked_at": rows[0].created_at.isoformat() if rows else None,
        "mode": "LIVE",
    }
