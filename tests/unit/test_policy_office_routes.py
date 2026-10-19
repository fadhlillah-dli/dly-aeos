"""§23: after a company authorisation exists there is a controlled path that makes a company-approved worker route
OFFICE-eligible (approved_worker_routes no longer stays empty by construction). Default deny everywhere else."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.models.decisions import Decision
from aeos.models.identity import User
from aeos.models.routing import ModelRoute
from aeos.policy import decisions as registry
from aeos.policy import office, office_routes, recording
from aeos.routing import service as routing
from tests.conftest import AuthedClient

ROUTE = "worker.office-free"


@pytest.fixture
def office_client(admin_client: AuthedClient) -> AuthedClient:
    admin_client.select_domain(Domain.OFFICE)
    return admin_client


def _owner(db) -> str:
    return db.scalar(select(User).where(User.username == "owner")).id


def _route(db) -> ModelRoute:
    r = ModelRoute(route_key=ROUTE, pool="WORKER", provider="openrouter", model_id="vendor/model:free",
                   status="CANDIDATE", eligible_domains=["PERSONAL"], is_free="YES", mode_params={})
    db.add(r)
    db.flush()
    r.config_hash = routing.compute_config_hash(r)
    db.commit()
    return r


def _authorise_company(db, *, approved_routes=(ROUTE,)) -> Decision:
    owner = _owner(db)
    recording.record_generic_decision(db, actor_user_id=owner, domain=Domain.OFFICE, kind="POLICY",
                                      rationale="rute worker disetujui perusahaan", auth_scope={"office": "routes"},
                                      office_policy={"approved_worker_routes": list(approved_routes),
                                                     "external_worker_allowed": True})
    auth = recording.record_generic_decision(db, actor_user_id=owner, domain=Domain.OFFICE, kind="POLICY",
                                             rationale="surat otorisasi perusahaan", auth_scope={"office": "auth"},
                                             office_authorization=True, company_reference="LETTER-2026-01",
                                             allowed_providers=["jira"])
    db.commit()
    return auth


BODY = {"route_key": ROUTE, "credential_ref": "office/openrouter", "rationale": "perusahaan menyetujui rute ini"}


def test_without_company_authorisation_the_route_stays_personal_only(office_client, db):
    _route(db)
    r = office_client.post("/api/policy/office/routes", json=BODY)
    assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_AUTHORISATION_REQUIRED"
    assert db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE)).eligible_domains == ["PERSONAL"]


def test_authorised_company_route_becomes_office_eligible_and_activatable(office_client, db):
    route = _route(db)
    _authorise_company(db)
    assert ROUTE in office.office_policy(db)["approved_worker_routes"]
    r = office_client.post("/api/policy/office/routes", json=BODY)
    assert r.status_code == 201, r.text
    d = r.json()["decision"]
    assert d["kind"] == "POLICY" and d["target_type"] == "office_route" and d["domain"] == "OFFICE"
    db.expire_all()
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    assert "OFFICE" in route.eligible_domains and route.mode_params["credential_ref"] == "office/openrouter"
    # an office/ credential never serves PERSONAL calls: the route is recorded OFFICE-only, not PERSONAL+OFFICE
    assert route.eligible_domains == ["OFFICE"]
    assert db.get(Decision, d["id"]).details["previous_eligible_domains"] == ["PERSONAL"]
    assert office_routes.office_route_authorised(db, route) is not None
    activated, decision = routing.activate_route(db, ROUTE, actor_user_id=_owner(db), mode="TRIAL",
                                                 rationale="uji rute office terbatas", expected_config_hash=route.config_hash)
    assert decision.kind == "TRIAL" and "OFFICE" in activated.eligible_domains


def test_route_not_company_approved_or_personal_credential_is_refused(office_client, db):
    _route(db)
    _authorise_company(db, approved_routes=("worker.other",))
    r = office_client.post("/api/policy/office/routes", json=BODY)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROUTE_NOT_COMPANY_APPROVED"
    _authorise_company(db)
    r = office_client.post("/api/policy/office/routes", json={**BODY, "credential_ref": "personal/openrouter"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "OFFICE_CREDENTIAL_REF_REQUIRED"


def test_personal_session_cannot_decide_office_eligibility(personal_client, db):
    _route(db)
    r = personal_client.post("/api/policy/office/routes", json=BODY)
    assert r.status_code == 403 and r.json()["error"]["code"] == "DOMAIN_MISMATCH"


def test_revoked_company_authorisation_ends_office_eligibility(office_client, db):
    _route(db)
    auth = _authorise_company(db)
    assert office_client.post("/api/policy/office/routes", json=BODY).status_code == 201
    registry.invalidate_decision(db, db.get(Decision, auth.id), reason="perusahaan mencabut otorisasi")
    db.commit()
    db.expire_all()
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE))
    assert office_routes.office_route_authorised(db, route) is None
    with pytest.raises(ValidationFailed):
        routing.activate_route(db, ROUTE, actor_user_id=_owner(db), mode="TRIAL", rationale="uji rute office terbatas",
                               expected_config_hash=route.config_hash)


def test_unauthorised_office_eligibility_set_by_hand_is_still_refused(db, admin_client):
    route = _route(db)
    route.eligible_domains = ["PERSONAL", "OFFICE"]
    route.config_hash = routing.compute_config_hash(route)
    db.commit()
    with pytest.raises(ValidationFailed):
        routing.activate_route(db, ROUTE, actor_user_id=_owner(db), mode="TRIAL", rationale="uji rute office terbatas",
                               expected_config_hash=route.config_hash)


def test_generic_decision_endpoint_cannot_write_the_dedicated_targets(office_client, db):
    """A POLICY decision on office_route/* or workforce/* through the generic endpoint would skip the company
    authorisation / approved-route / evaluation checks of the dedicated endpoints; it is refused."""
    _route(db)
    _authorise_company(db)
    auth_id = office.company_authorisation(db).id
    for target_type, target_id, details in (
            ("office_route", ROUTE, {"company_authorisation_id": auth_id, "credential_ref": "office/openrouter"}),
            ("workforce", "concurrency", {"max_active_workers": 2})):
        r = office_client.post("/api/decisions", json={
            "kind": "POLICY", "rationale": "langsung lewat endpoint generik", "auth_scope": {"scope": "test"},
            "target_type": target_type, "target_id": target_id, "target_hash": "sha256:x", "details": details})
        assert r.status_code == 422 and r.json()["error"]["code"] == "DEDICATED_ENDPOINT_REQUIRED", r.text
    assert db.scalar(select(Decision).where(Decision.target_type.in_(["office_route", "workforce"]))) is None
    # the dedicated path still works
    assert office_client.post("/api/policy/office/routes", json=BODY).status_code == 201
