"""Metadata search (§35): active domain only; counts/metadata never leak other-domain rows (§50)."""

from __future__ import annotations

from aeos.core.enums import Domain
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.inventory import ComponentReference
from aeos.models.planning import Plan
from aeos.models.publishing import DocumentRecord
from tests.conftest import VIEWER_PW, AuthedClient
from tests.unit.test_portfolio_support import create_project


def _seed(admin_client: AuthedClient, db) -> tuple[dict, dict]:
    admin_client.select_domain(Domain.OFFICE)
    office = create_project(admin_client, domain=Domain.OFFICE, name="Zephyr Office Ledger")
    admin_client.select_domain(Domain.PERSONAL)
    personal = create_project(admin_client, name="Zephyr Personal Notes")
    db.add_all([
        Plan(project_id=office["id"], domain="OFFICE", title="Zephyr office rollout"),
        Plan(project_id=personal["id"], domain="PERSONAL", title="Zephyr personal plan"),
        Decision(kind="POLICY", project_id=office["id"], domain="OFFICE", actor_user_id="u", target_type="policy",
                 target_id="office", rationale="Zephyr office authorisation"),
        Decision(kind="ADR", project_id=None, domain=None, actor_user_id="u", target_type="adr", target_id="ADR-0001",
                 rationale="Zephyr global platform decision"),
        DocumentRecord(project_id=office["id"], domain="OFFICE", logical_doc_id="00-product-home", node="00",
                       title="Zephyr Office Home"),
        DocumentRecord(project_id=personal["id"], domain="PERSONAL", logical_doc_id="00-product-home", node="00",
                       title="Zephyr Personal Home"),
        ComponentReference(component_id="zephyr-office-tool", kind="tool", name="Zephyr Office Tool",
                           scope={"domains": ["OFFICE"]}),
        ComponentReference(component_id="zephyr-global", kind="tool", name="Zephyr Global Tool", scope={}),
        ArchitectureDecisionRecord(adr_key="ADR-0099", title="Zephyr ADR", status="ACCEPTED"),
    ])
    db.commit()
    return office, personal


def test_search_scoped_to_active_domain(admin_client: AuthedClient, db):
    office, personal = _seed(admin_client, db)
    r = admin_client.get("/api/search", params={"q": "zephyr"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Office" not in r.text and office["id"] not in r.text
    assert body["counts"]["project"] == 1 and body["counts"]["plan"] == 1 and body["counts"]["document"] == 1
    assert body["counts"]["decision"] == 1  # only the global one
    assert body["counts"]["component"] == 1 and body["counts"]["adr"] == 1
    assert body["total"] == sum(body["counts"].values()) == len(body["items"])
    assert body["domain"] == "PERSONAL" and body["source"] and body["checked_at"]
    kinds = {i["kind"] for i in body["items"]}
    assert {"project", "plan", "document", "decision", "component", "adr"} <= kinds
    assert all(i["project_id"] in (None, personal["id"]) for i in body["items"])

    admin_client.select_domain(Domain.OFFICE)
    body = admin_client.get("/api/search", params={"q": "zephyr"}).json()
    assert "Personal" not in str(body["items"])
    assert body["counts"]["project"] == 1 and body["counts"]["decision"] == 2 and body["counts"]["component"] == 2


def test_search_pagination_and_kind_filter(personal_client: AuthedClient):
    for i in range(5):
        create_project(personal_client, name=f"Paginate {i}")
    page1 = personal_client.get("/api/search", params={"q": "paginate", "kind": "project", "limit": 2}).json()
    page3 = personal_client.get("/api/search", params={"q": "paginate", "kind": "project", "limit": 2, "offset": 4}).json()
    assert page1["total"] == 5 and len(page1["items"]) == 2 and page1["has_more"] is True
    assert len(page3["items"]) == 1 and page3["has_more"] is False
    assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page3["items"]})
    assert personal_client.get("/api/search", params={"q": "paginate", "kind": "secrets"}).status_code == 422
    assert personal_client.get("/api/search", params={"q": "p"}).status_code == 422


def test_like_wildcards_are_literal(personal_client: AuthedClient):
    create_project(personal_client, name="Plain name")
    body = personal_client.get("/api/search", params={"q": "%%", "kind": "project"}).json()
    assert body["total"] == 0
    body = personal_client.get("/api/search", params={"q": "__", "kind": "project"}).json()
    assert body["total"] == 0


def test_search_requires_domain(admin_client: AuthedClient):
    r = admin_client.get("/api/search", params={"q": "anything"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_project_scoped_user_sees_only_scoped_rows(admin_client: AuthedClient, db):
    from aeos.models.identity import User
    from tests.conftest import create_viewer

    admin_client.select_domain(Domain.PERSONAL)
    a = create_project(admin_client, name="Scoped Alpha")
    b = create_project(admin_client, name="Scoped Beta")
    viewer = create_viewer(db)
    u = db.get(User, viewer.id)
    u.project_scope = [a["id"]]
    db.commit()
    admin_client.post("/api/auth/logout")
    admin_client.csrf = None
    admin_client.login("viewer", VIEWER_PW)
    admin_client.select_domain(Domain.PERSONAL)
    body = admin_client.get("/api/search", params={"q": "scoped"}).json()
    assert body["counts"]["project"] == 1 and body["items"][0]["id"] == a["id"]
    assert b["id"] not in str(body)
    assert admin_client.get(f"/api/projects/{b['id']}").status_code == 404
