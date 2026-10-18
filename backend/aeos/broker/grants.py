"""Scoped, run-bound capability grants (§25, §26).

* The raw token (``aeoscap_<urlsafe>``) is returned once to the caller that hands it to the
  sandbox; only its SHA-256 is stored. The raw value is registered for redaction.
* Grants are bound to one run, project and domain, one kind, a scope, caps and an expiry.
* Usage counters are updated with single conditional UPDATE statements so concurrent requests
  cannot pass a cap (§14 "concurrency tidak boleh membuka cara melewati batas").
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Forbidden, Unauthenticated, ValidationFailed
from aeos.core.hashing import canonical_json
from aeos.core.redaction import contains_secret, register_secret
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.routing import CapabilityGrant

KINDS = frozenset({"MODEL_PROXY", "GIT_BRANCH", "PR_CREATE", "READ_CONTEXT"})
TOKEN_PREFIX = "aeoscap_"
MIN_TTL = 30
MAX_TTL = 8 * 3600


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_scope(kind: str, scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scope, dict):
        raise ValidationFailed("scope must be an object")
    if kind == "MODEL_PROXY":
        models = scope.get("models")
        if not isinstance(models, dict) or not models:
            raise ValidationFailed("MODEL_PROXY scope needs a non-empty models map {exposed_id: route_key}")
    elif kind == "GIT_BRANCH":
        if not str(scope.get("branch", "")).startswith("aeos/") or not scope.get("repo"):
            raise ValidationFailed("GIT_BRANCH scope needs repo and an aeos/* branch")
    elif kind == "PR_CREATE":
        if not scope.get("repo") or not str(scope.get("head", "")).startswith("aeos/") or not scope.get("base"):
            raise ValidationFailed("PR_CREATE scope needs repo, aeos/* head and base")
    elif kind == "READ_CONTEXT":
        if not isinstance(scope.get("paths"), list):
            raise ValidationFailed("READ_CONTEXT scope needs a paths list")
    if contains_secret(canonical_json(scope)):
        raise ValidationFailed("capability scope must not contain credentials")
    return dict(scope)


def issue_grant(
    session: Session, *, run_id: str, project_id: str, domain: str, kind: str, scope: dict[str, Any],
    ttl_seconds: int, max_requests: int | None = None, max_tokens: int | None = None,
    max_cost_usd: float | None = None, actor: str = "service:broker",
) -> tuple[str, CapabilityGrant]:
    if kind not in KINDS:
        raise ValidationFailed(f"unknown capability kind {kind!r}")
    Domain(domain)  # raises on an invalid domain
    if not run_id or not project_id:
        raise ValidationFailed("grants are always bound to a run and a project")
    ttl = int(ttl_seconds)
    if ttl < MIN_TTL or ttl > MAX_TTL:
        raise ValidationFailed(f"ttl_seconds must be within {MIN_TTL}..{MAX_TTL}")
    for name, cap in (("max_requests", max_requests), ("max_tokens", max_tokens), ("max_cost_usd", max_cost_usd)):
        if cap is not None and cap < 0:
            raise ValidationFailed(f"{name} must be >= 0")
    clean_scope = _validate_scope(kind, scope)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    register_secret(raw)
    grant = CapabilityGrant(
        token_hash=hash_token(raw), run_id=run_id, project_id=project_id, domain=domain, kind=kind,
        scope=clean_scope, max_requests=max_requests, max_tokens=max_tokens,
        max_cost_usd=max_cost_usd, used_requests=0, used_tokens=0, used_cost_usd=0,
        expires_at=utcnow() + timedelta(seconds=ttl), active=True,
    )
    session.add(grant)
    session.flush()
    audit.record(session, actor_type="service", actor_id=actor, action="broker.grant.issue", target_type="capability_grant",
                 target_id=grant.id, project_id=project_id, domain=domain,
                 details={"run_id": run_id, "kind": kind, "scope": clean_scope, "ttl_seconds": ttl,
                          "caps": {"requests": max_requests, "tok": max_tokens, "usd": max_cost_usd}})
    events.emit(session, source="broker", kind="broker.grant.issued", entity_type="capability_grant",
                entity_id=grant.id, project_id=project_id, domain=domain, run_id=run_id,
                payload={"kind": kind, "expires_at": grant.expires_at.isoformat()})
    return raw, grant


def revoke_grants(session: Session, *, run_id: str | None = None, all_active: bool = False, reason: str,
                  actor: str = "service:broker") -> int:
    if not run_id and not all_active:
        raise ValidationFailed("revoke_grants needs run_id or all_active=True")
    q = select(CapabilityGrant).where(CapabilityGrant.active.is_(True))
    if run_id:
        q = q.where(CapabilityGrant.run_id == run_id)
    rows = list(session.scalars(q))
    now = utcnow()
    for g in rows:
        g.active = False
        g.revoked_at = now
        g.revoked_reason = (reason or "revoked")[:128]
    session.flush()
    for g in rows:
        audit.record(session, actor_type="service", actor_id=actor, action="broker.grant.revoke",
                     target_type="capability_grant", target_id=g.id, project_id=g.project_id, domain=g.domain,
                     reason=reason, details={"run_id": g.run_id, "kind": g.kind})
    if rows:
        events.emit(session, source="broker", kind="broker.grant.revoked", entity_type="run",
                    entity_id=run_id or "*", run_id=run_id, project_id=rows[0].project_id, domain=rows[0].domain,
                    payload={"count": len(rows), "reason": reason})
    return len(rows)


def authenticate(session: Session, raw_token: str | None, *, kind: str) -> CapabilityGrant:
    """Resolve a bearer token to an ACTIVE, unexpired grant of *kind* (fail closed)."""
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        raise Unauthenticated("missing or malformed capability token", code="CAPABILITY_TOKEN_INVALID")
    grant = session.scalar(select(CapabilityGrant).where(CapabilityGrant.token_hash == hash_token(raw_token)))
    if grant is None:
        raise Unauthenticated("unknown capability token", code="CAPABILITY_TOKEN_INVALID")
    if not grant.active or grant.revoked_at is not None:
        raise Unauthenticated("capability token revoked", code="CAPABILITY_TOKEN_REVOKED")
    exp = ensure_aware(grant.expires_at)
    if exp is None or exp <= utcnow():
        grant.active = False
        grant.revoked_reason = grant.revoked_reason or "expired"
        session.flush()
        raise Unauthenticated("capability token expired", code="CAPABILITY_TOKEN_EXPIRED")
    if grant.kind != kind:
        raise Forbidden(f"capability token is not valid for {kind}", code="CAPABILITY_KIND_MISMATCH")
    return grant


def consume_request(session: Session, grant_id: str) -> bool:
    """Atomically count one request against max_requests. False when the cap is exhausted."""
    res = session.execute(
        update(CapabilityGrant)
        .where(CapabilityGrant.id == grant_id, CapabilityGrant.active.is_(True),
               or_(CapabilityGrant.max_requests.is_(None), CapabilityGrant.used_requests < CapabilityGrant.max_requests))
        .values(used_requests=CapabilityGrant.used_requests + 1)
    )
    return res.rowcount == 1


def add_usage(session: Session, grant_id: str, *, tokens: int, cost_usd: float | None) -> None:
    values: dict[str, Any] = {"used_tokens": CapabilityGrant.used_tokens + max(0, int(tokens))}
    if cost_usd is not None:
        values["used_cost_usd"] = CapabilityGrant.used_cost_usd + Decimal(str(max(0.0, float(cost_usd))))
    session.execute(update(CapabilityGrant).where(CapabilityGrant.id == grant_id).values(**values))


def remaining(grant: CapabilityGrant) -> dict[str, Any]:
    def left(cap: Any, used: Any) -> float | None:
        return None if cap is None else float(cap) - float(used or 0)

    return {"requests": left(grant.max_requests, grant.used_requests), "tokens": left(grant.max_tokens, grant.used_tokens),
            "cost_usd": left(grant.max_cost_usd, grant.used_cost_usd)}


def grant_view(g: CapabilityGrant) -> dict[str, Any]:
    """Admin view — never includes the token or its hash."""
    return {
        "id": g.id, "run_id": g.run_id, "project_id": g.project_id, "domain": g.domain, "kind": g.kind,
        "scope": g.scope, "caps": {"max_requests": g.max_requests, "max_tokens": g.max_tokens,
                                   "max_cost_usd": float(g.max_cost_usd) if g.max_cost_usd is not None else None},
        "used": {"requests": g.used_requests, "tokens": g.used_tokens, "cost_usd": float(g.used_cost_usd or 0)},
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
        "revoked_reason": g.revoked_reason, "active": bool(g.active and g.revoked_at is None
                                                           and ensure_aware(g.expires_at) > utcnow()),
    }
