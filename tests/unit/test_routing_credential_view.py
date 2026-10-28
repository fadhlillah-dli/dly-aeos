"""The route view carries a credential block, and a keyless route is not counted as active (R28 round 3).

Incident 2026-10-28: a TRIAL route with an ACTIVE decision but no stored key was rejected by the router (Doctor
CAN_RUN_WORKER BLOCKED) while the Models page drew it green "Aktif · TRIAL", counted it in "Aktif: N" and sorted it
first. The view now states {state, ref, reason} from the router's own credential rules; ``counts.approved`` requires
PRESENT or NOT_NEEDED. Hermetic: an in-memory store or a counting fake (the SecretGuard hides the real ones); no value
is read and no provider is called.
"""

from __future__ import annotations

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.routing import seed, service
from tests.conftest import AuthedClient
from tests.unit.test_routing_fakes import install_fakes
from tests.unit.test_routing_key_eligibility import CountingStore, _install_store
from tests.unit.test_routing_service import DS, approve

ZEN_REF = "workers/opencode-zen"
LOCAL = "local/qwen3.8-flash-next"
NATIVE = "claude-native/opus"


def _seed(db) -> None:
    seed.run(db)
    for r in db.query(service.ModelRoute).all():
        r.pricing_checked_at = r.terms_checked_at = utcnow()
        r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
    db.commit()


def _list(client: AuthedClient) -> tuple[dict, dict[str, dict]]:
    r = client.get("/api/routes")
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {i["route_key"]: i for i in body["items"]}


def test_bite_a_keyless_trial_route_is_missing_and_not_counted(personal_client: AuthedClient, db, monkeypatch,
                                                                 secrets_memory):
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    body, by = _list(personal_client)
    cred = by[DS]["credential"]
    assert by[DS]["status"] == "TRIAL" and by[DS]["active_decision"], by[DS]
    assert cred["state"] == "MISSING" and cred["ref"] == ZEN_REF and cred["domain"] == "PERSONAL", cred
    assert f"not stored: aeos secrets set {ZEN_REF}" in cred["reason"]
    assert body["counts"]["approved"] == 0, "a keyless TRIAL route must not be counted as active"
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert detail["credential"]["state"] == "MISSING"

    # near miss: storing the key is the only change; the route is then PRESENT and counted
    secrets_memory.put(ZEN_REF, "fake-zen-key-value", actor="test")
    body, by = _list(personal_client)
    assert by[DS]["credential"] == {"state": "PRESENT", "ref": ZEN_REF, "reason": None, "domain": "PERSONAL"}
    assert body["counts"]["approved"] == 1


def test_local_and_native_routes_need_no_key_and_count_as_active(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    store = CountingStore(set(), down=True)  # even a sealed store: no lookup for a route that needs no key
    _install_store(monkeypatch, store)
    _seed(db)
    approve(db, LOCAL, kind="TRIAL")
    db.commit()
    body, by = _list(personal_client)
    for key in (LOCAL, NATIVE):
        assert by[key]["credential"] == {"state": "NOT_NEEDED", "ref": None, "reason": None, "domain": "PERSONAL"}, key
    assert by[LOCAL]["active_decision"] and body["counts"]["approved"] == 1


def test_bite_sealed_store_is_unavailable_and_looked_up_once(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    store = CountingStore({ZEN_REF}, down=True)
    _install_store(monkeypatch, store)
    body, by = _list(personal_client)
    cred = by[DS]["credential"]
    assert cred["state"] == "UNAVAILABLE" and cred["ref"] == ZEN_REF, cred
    assert "cannot be checked: secret store unavailable" in cred["reason"]
    assert body["counts"]["approved"] == 0
    assert len(store.calls) == 1, store.calls  # the first failure short-circuits every other reference

    # near miss: the store answers again and the key is stored
    store.down = False
    store.calls.clear()
    body, by = _list(personal_client)
    assert by[DS]["credential"]["state"] == "PRESENT" and body["counts"]["approved"] == 1
    assert len(store.calls) == len(set(store.calls)), f"one lookup per reference per list call: {store.calls}"


def test_office_session_never_looks_up_personal_references(admin_client: AuthedClient, db, monkeypatch):
    """L12: the credential block is evaluated for the session's domain; an OFFICE view does not reveal whether a
    PERSONAL/worker key is stored (no lookup at all), and a PERSONAL-only route is MISSING for OFFICE."""
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    store = CountingStore({ZEN_REF, "workers/openrouter"})
    _install_store(monkeypatch, store)
    admin_client.select_domain(Domain.OFFICE)
    body, by = _list(admin_client)
    assert by[DS]["credential"]["state"] == "MISSING" and by[DS]["credential"]["domain"] == "OFFICE"
    assert not [c for c in store.calls if not c.startswith("office/")], store.calls
    assert body["counts"]["approved"] == 0
    admin_client.select_domain(Domain.PERSONAL)
    _, by = _list(admin_client)
    assert by[DS]["credential"]["state"] == "PRESENT"
