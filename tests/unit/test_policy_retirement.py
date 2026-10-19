"""§33 retirement ends with the legitimate owner's final verification; RETIRED only after it.

The RETIREMENT decision is human AUTH bound to the project and the hash of the checklist the owner verified.
It is refused while an item is BLOCKING; a checklist that changes afterwards makes it STALE (no RETIRED).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision
from aeos.models.portfolio import ProjectIntegration
from aeos.models.routing import CapabilityGrant
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project

RATIONALE = "semua butir retirement sudah saya periksa"


def _retiring(client: AuthedClient, name: str = "Pensiun") -> str:
    pid = create_project(client, name=name)["id"]
    assert client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRING"}).status_code == 200
    return pid


def test_retired_is_refused_without_owner_final_verification(personal_client: AuthedClient):
    pid = _retiring(personal_client)
    r = personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_NOT_VERIFIED"
    final = {i["key"]: i for i in personal_client.get(f"/api/ops/retirement/{pid}").json()["items"]}
    assert final["final_verification"]["status"] == "PENDING_OWNER"


def test_owner_decision_verifies_and_unlocks_retired(personal_client: AuthedClient, db):
    pid = _retiring(personal_client)
    shown = personal_client.get(f"/api/ops/retirement/{pid}").json()
    r = personal_client.post(f"/api/ops/retirement/{pid}/decision",
                             json={"rationale": RATIONALE, "checklist_hash": shown["checklist_hash"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["decision"]["kind"] == "RETIREMENT" and body["decision"]["target_hash"] == shown["checklist_hash"]
    assert {i["key"]: i for i in body["checklist"]["items"]}["final_verification"]["status"] == "VERIFIED"
    d = db.scalar(select(Decision).where(Decision.kind == "RETIREMENT", Decision.target_id == pid))
    assert d is not None and d.actor_kind == "HUMAN" and d.allowed_operations == ["retire_project"]
    r = personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 200 and r.json()["project"]["lifecycle"] == "RETIRED"


def test_decision_refused_while_checklist_is_blocking(personal_client: AuthedClient, db):
    pid = _retiring(personal_client, "Masih aktif")
    db.add(CapabilityGrant(token_hash="h-ret", run_id="r", project_id=pid, domain="PERSONAL", kind="GIT_BRANCH",
                           expires_at=utcnow() + timedelta(hours=1)))
    db.commit()
    r = personal_client.post(f"/api/ops/retirement/{pid}/decision", json={"rationale": RATIONALE})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_BLOCKING_ITEMS"
    assert "credentials" in r.json()["error"]["details"]["blocking"]
    assert db.scalar(select(Decision).where(Decision.kind == "RETIREMENT")) is None


def test_checklist_change_after_verification_makes_it_stale(personal_client: AuthedClient, db):
    pid = _retiring(personal_client, "Berubah")
    assert personal_client.post(f"/api/ops/retirement/{pid}/decision",
                                json={"rationale": RATIONALE}).status_code == 201
    # an integration appears after the owner verified the checklist: the binding no longer matches
    db.add(ProjectIntegration(project_id=pid, domain="PERSONAL", provider="github", purpose="code"))
    db.commit()
    items = {i["key"]: i for i in personal_client.get(f"/api/ops/retirement/{pid}").json()["items"]}
    assert items["final_verification"]["status"] in ("STALE", "PENDING_OWNER")
    r = personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_NOT_VERIFIED"


def test_stale_checklist_hash_and_non_retiring_project_are_refused(personal_client: AuthedClient):
    pid = create_project(personal_client, name="Aktif")["id"]
    r = personal_client.post(f"/api/ops/retirement/{pid}/decision", json={"rationale": RATIONALE})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_REQUIRES_RETIRING"
    assert personal_client.patch(f"/api/projects/{pid}", json={"lifecycle": "RETIRING"}).status_code == 200
    r = personal_client.post(f"/api/ops/retirement/{pid}/decision",
                             json={"rationale": RATIONALE, "checklist_hash": "sha256:old"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETIREMENT_CHECKLIST_STALE"


def test_retirement_is_not_a_generic_decision_kind(personal_client: AuthedClient):
    from aeos.policy import catalog

    assert "RETIREMENT" in catalog.DECISION_KINDS and "RETIREMENT" not in catalog.GENERIC_DECISION_KINDS
