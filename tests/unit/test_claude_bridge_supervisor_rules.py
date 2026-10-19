"""Claude supervisor rules (master §01 U-06 "Claude Max 20x via the official Claude Code is the supervisor",
§12 "no platform-made Claude login form", §12 future multi-user "one Max subscription is not a shared
backend")."""

from __future__ import annotations

from aeos.claude_bridge import binary as binmod
from aeos.claude_bridge import bridge
from aeos.claude_bridge.binary import AuthInfo, BinaryInfo, Probe
from aeos.claude_bridge.facade import ClaudeRequest
from aeos.core.enums import Role
from tests.conftest import ADMIN_PW, AuthedClient, create_admin
from tests.unit.test_claude_bridge_parsing import SCHEMA

PROMPT = "Kembalikan {\"ok\": true}."


def _pro_probe() -> Probe:
    from pathlib import Path

    return Probe(binary=BinaryInfo(found=True, path=Path("claude.exe"), source="PATH", version="2.1.302",
                                   fingerprint="sha256:" + "a" * 64, reasons=[]),
                 auth=AuthInfo(status="DEGRADED", logged_in=True, auth_method="claude.ai", api_provider="firstParty",
                               subscription_type="pro", email="owner@example.com",
                               reasons=["subscriptionType is 'pro'"]), checked_at=0.0)


def test_non_max_login_is_not_used_as_the_supervisor_without_an_owner_exception(settings, db, monkeypatch):
    called: list[int] = []

    def no_run(*a, **k):
        called.append(1)
        raise RuntimeError("stop after the gate")

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _pro_probe())
    monkeypatch.setattr(bridge, "run_process", no_run)
    req = ClaudeRequest(purpose="planning.primary", role="planner", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                        domain="PERSONAL")
    res = bridge.run_structured(req, settings=settings)
    assert not res.ok and res.error_code == "SUBSCRIPTION_NOT_MAX" and "manual package" in res.error
    assert not called  # the binary never ran
    from aeos.policy import facade as policy

    owner = create_admin(db)
    policy.record_decision(db, kind="EXCEPTION", actor_user_id=owner.id, target_type=bridge.NON_MAX_TARGET[0],
                           target_id=bridge.NON_MAX_TARGET[1], rationale="owner accepts Pro login for a trial week")
    db.commit()
    try:
        bridge.run_structured(req, settings=settings)
    except RuntimeError:
        pass
    assert called, "with the owner exception the gate lets the request through"


def test_capability_test_is_reserved_to_the_bootstrap_owner(client: AuthedClient, db, monkeypatch):
    from aeos.identity import service as identity

    create_admin(db)
    identity.create_user(db, username="second-admin", password="another-strong-pass-31", role=Role.SUPER_ADMIN)
    db.commit()
    monkeypatch.setattr(bridge, "capability_test", lambda model, actor, settings: {"status": "VERIFIED", "id": "t1"})
    client.login("second-admin", "another-strong-pass-31")
    r = client.post("/api/claude/capability-test", json={"model": "opus"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BRIDGE_OWNER_ONLY"
    client.login("owner", ADMIN_PW)
    r = client.post("/api/claude/capability-test", json={"model": "opus"})
    assert r.status_code == 200 and r.json()["status"] == "VERIFIED"


def test_no_claude_route_accepts_a_credential(app):
    """AEOS never offers its own Claude login: no /api/claude body field carries a token/password/code."""
    spec = app.openapi()
    forbidden = ("token", "password", "passwd", "code", "credential", "api_key", "apikey", "secret", "cookie")
    for path, ops in spec["paths"].items():
        if not path.startswith("/api/claude"):
            continue
        assert "login" not in path.lower()
        for op in ops.values():
            ref = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema", {})
            name = ref.get("$ref", "").rsplit("/", 1)[-1]
            props = (spec["components"]["schemas"].get(name, {}) if name else ref).get("properties", {})
            bad = [p for p in props if any(f in p.lower() for f in forbidden)]
            assert not bad, (path, bad)
