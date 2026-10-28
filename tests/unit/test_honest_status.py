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


def test_routes_list_counts_the_active_route_and_carries_its_expiry(personal_client: AuthedClient, db, monkeypatch,
                                                                    worker_keys):
    # worker_keys: the route's key is stored (a keyless route is not counted: test_routing_credential_view.py)
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


def test_route_whose_trial_decision_expired_is_not_counted_active(personal_client: AuthedClient, db, monkeypatch,
                                                                  worker_keys):
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


def _comp(requirement: str = "OPTIONAL", probe: dict | None = None):
    # probeable by default: INSTALL_TRIAL ("pasang lalu probe") is only satisfiable for a component reconcile can observe
    return SimpleNamespace(component_id="hermes", tombstoned_at=None, requirement=requirement,
                           probe={"type": "binary"} if probe is None else probe)


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
    assert action["code"] == "INSTALL_TRIAL"
    # the label follows the admission: only TRIAL_APPROVED is called a trial
    expected = "Trial disetujui: pasang lalu probe" if admission == "TRIAL_APPROVED" else "Disetujui: pasang lalu probe"
    assert action["label"] == expected
    assert action["command"] is None  # manual step: no auto-install (AGENTS.md invariant 8)


def test_install_trial_near_misses_stay_monitor():
    assert _code("TRIAL_APPROVED", "INSTALLED") == "MONITOR"  # installed: nothing left to install
    assert _code("DISCOVERED", "UNKNOWN") != "INSTALL_TRIAL"  # no approval, no install request
    assert _code("UNDER_REVIEW", "NOT_INSTALLED") != "INSTALL_TRIAL"
    assert _code("TRIAL_APPROVED", "INSTALL_FAILED") != "INSTALL_TRIAL"  # a failed install is not "not installed yet"


@pytest.mark.parametrize("admission", ["TRIAL_APPROVED", "APPROVED"])
def test_admitted_without_probe_is_asked_for_a_probe_not_an_install(admission):
    action = views.next_action(_comp(probe={}), _dims(admission, "UNKNOWN"), [], {"status": "NO_PIN"})
    assert action["code"] == "ADD_PROBE", action
    assert action["command"] is None and action["link"] == "/readiness"
    # an older NOT_INSTALLED observation of a component whose probe is now none: still no install-and-probe request
    stale = views.next_action(_comp(probe={}), _dims(admission, "NOT_INSTALLED"), [], {"status": "NO_PIN"})
    assert stale["code"] == "ADD_PROBE", f"unprobeable admitted NOT_INSTALLED component asked to {stale['code']}"
    # near misses: a probeable admitted component still asks to install; an unadmitted unprobed one is no probe request
    assert _code(admission, "UNKNOWN") == "INSTALL_TRIAL"
    assert views.next_action(_comp(probe={}), _dims("DISCOVERED", "UNKNOWN"), [], {"status": "NO_PIN"})["code"] \
        != "ADD_PROBE"


@pytest.mark.parametrize("admission", ["TRIAL_APPROVED", "APPROVED"])
def test_admitted_outside_the_probe_domain_is_not_asked_to_install_and_probe(admission):
    """An admission reconcile does not follow (an OFFICE decision, ``unadmitted``) can never be probed, so the
    satisfiable step is a manual check, not INSTALL_TRIAL "pasang lalu probe"."""
    dims = _dims(admission, "UNKNOWN")
    dims["installation"] = {"value": "UNKNOWN", "observation_id": None, "stale": False}  # never probed
    action = views.next_action(_comp(), dims, [], {"status": "NO_PIN"}, unadmitted=True)
    assert action["code"] == "OUTSIDE_PROBE_DOMAIN", f"unprobeable admission is asked for {action['code']}"
    assert action["command"] is None
    # near misses: the same admission that reconcile follows still asks to install; an unadmitted candidate under
    # review is still a review, not an outside-domain notice
    assert views.next_action(_comp(), dims, [], {"status": "NO_PIN"})["code"] == "RUN_RECONCILE"
    assert _code(admission, "UNKNOWN") == "INSTALL_TRIAL"
    assert views.next_action(_comp("CANDIDATE"), _dims("UNDER_REVIEW", "UNKNOWN"), [], {"status": "NO_PIN"},
                             unadmitted=True)["code"] == "REVIEW_CANDIDATE"
