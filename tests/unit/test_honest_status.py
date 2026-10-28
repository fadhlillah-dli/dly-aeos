"""R28 (2026-10-28): the Control Center showed activated items as not active. Backend side of the fix.

* /api/routes already carries what the UI needs to say "active": ``counts.approved`` (TRIAL + APPROVED),
  ``active_decision.expires_at`` (only from an ACTIVE, human, unexpired decision bound to the current config hash) and
  ``free_tier`` (only for an OpenRouter ``:free`` route). These tests pin them so a refactor cannot drop them.
* ``next_action`` for a trial- or fully-approved component that is not installed is INSTALL_TRIAL, never MONITOR.
  It does not change enablement (master section 38: a TRIAL decision is not an enablement basis).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aeos.inventory import views
from tests.conftest import AuthedClient
from tests.unit.test_routing_api import DS, LAGUNA, _seed
from tests.unit.test_routing_fakes import OWNER_CLIENT_TERMS as CT
from tests.unit.test_routing_fakes import install_fakes


def test_routes_list_counts_the_active_route_and_carries_its_expiry(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    before = personal_client.get("/api/routes").json()
    assert before["counts"]["approved"] == 0 and all(i["active_decision"] is None for i in before["items"])
    h = next(i for i in before["items"] if i["route_key"] == DS)["config_hash"]
    r = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "pilot for slice 1", "expected_config_hash": h, "client_terms": CT})
    assert r.status_code == 200, r.text
    body = personal_client.get("/api/routes").json()
    assert body["counts"]["approved"] == 1 and body["counts"]["total"] == len(body["items"])
    by_key = {i["route_key"]: i for i in body["items"]}
    active = by_key[DS]
    assert active["status"] == "TRIAL" and active["active_decision"]["kind"] == "TRIAL"
    assert active["active_decision"]["expires_at"], "a TRIAL activation carries its expiry date"
    candidates = [i for k, i in by_key.items() if k != DS]
    assert candidates and all(i["status"] != "TRIAL" and i["active_decision"] is None for i in candidates)


def test_route_whose_trial_decision_expired_is_not_counted_active(personal_client: AuthedClient, db, monkeypatch):
    """Review of dea380c: nothing resets ModelRoute.status when its decision stops being ACTIVE, so a TRIAL route can
    keep status TRIAL with active_decision null. It must not be counted as active (the router rejects it)."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.models.decisions import Decision

    install_fakes(monkeypatch)
    _seed(db)
    h = next(i for i in personal_client.get("/api/routes").json()["items"] if i["route_key"] == DS)["config_hash"]
    r = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "pilot for slice 1", "expected_config_hash": h, "client_terms": CT})
    assert r.status_code == 200, r.text
    assert personal_client.get("/api/routes").json()["counts"]["approved"] == 1  # near miss: bound decision counts
    d = db.query(Decision).filter(Decision.target_id == DS, Decision.kind == "TRIAL").one()
    # the 7 days passed and expire_due_decisions persisted EXPIRED (install_fakes replaces the policy module, so the
    # same two writes are made by hand); like the real one it never touches ModelRoute.status
    d.expires_at, d.status = utcnow() - timedelta(days=1), "EXPIRED"
    db.commit()
    body = personal_client.get("/api/routes").json()
    item = next(i for i in body["items"] if i["route_key"] == DS)
    assert item["status"] == "TRIAL" and item["active_decision"] is None  # status is never reset by expiry
    assert body["counts"]["approved"] == 0 and body["counts"]["total"] == len(body["items"])


def test_free_tier_is_present_only_for_a_free_model_route(personal_client: AuthedClient, db):
    _seed(db)
    items = {i["route_key"]: i for i in personal_client.get("/api/routes").json()["items"]}
    tier = items[LAGUNA]["free_tier"]
    assert tier["state"] in ("OK", "LIMIT_REACHED") and tier["requests_today_utc"] == 0
    assert tier["daily_limit"] > 0 and tier["rpm_limit"] > 0 and tier["counted"]
    assert items[DS]["free_tier"] is None  # an OpenCode Zen route is not an OpenRouter :free route


def _comp(requirement: str = "OPTIONAL"):
    return SimpleNamespace(component_id="hermes", tombstoned_at=None, requirement=requirement, probe={})


def _dims(admission: str, installation: str, **over):
    base = {
        "admission": {"value": admission},
        "installation": {"value": installation, "observation_id": "o-1", "stale": False},
        "connectivity": {"value": "NOT_CHECKED"},
        "lifecycle_final": {"value": "ACTIVE"},
    }
    base.update(over)
    return base


def _code(admission: str, installation: str, requirement: str = "OPTIONAL") -> str:
    return views.next_action(_comp(requirement), _dims(admission, installation), [], {"status": "NO_PIN"})["code"]


@pytest.mark.parametrize("admission", ["TRIAL_APPROVED", "APPROVED"])
@pytest.mark.parametrize("installation", ["UNKNOWN", "NOT_INSTALLED"])
def test_approved_but_not_installed_asks_to_install_not_to_monitor(admission, installation):
    action = views.next_action(_comp(), _dims(admission, installation), [], {"status": "NO_PIN"})
    assert action["code"] == "INSTALL_TRIAL" and action["label"] == "Trial disetujui: pasang lalu probe"
    assert action["command"] is None  # manual step: no auto-install (AGENTS.md invariant 8)


def test_install_trial_near_misses_stay_monitor():
    assert _code("TRIAL_APPROVED", "INSTALLED") == "MONITOR"  # installed: nothing left to install
    assert _code("DISCOVERED", "UNKNOWN") != "INSTALL_TRIAL"  # no approval, no install request
    assert _code("UNDER_REVIEW", "NOT_INSTALLED") != "INSTALL_TRIAL"
    assert _code("TRIAL_APPROVED", "INSTALL_FAILED") != "INSTALL_TRIAL"  # a failed install is not "not installed yet"
