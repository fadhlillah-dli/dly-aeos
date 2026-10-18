"""Publisher + docs API: RBAC, domain scoping, interlock, separate status fields, no secrets (§26, §35, §41, §50).

§50 rows: "VIEWER memanggil write endpoint langsung → Ditolak backend", "Dashboard Personal mencoba run/evidence
Office → Backend menolak sesuai scope", "Bootstrap belum selesai tetapi publish diminta → Interlock menolak".
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.models.audit import AuditEvent
from aeos.models.publishing import DocumentRecord, DocumentRevision, OperationalWorkProjection
from aeos.publisher import jobs
from tests.conftest import VIEWER_PW, create_viewer
from tests.unit.test_publisher_support import (
    FakeProvider,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)

SECRET_MARKERS = ("ya29.test-token", "1//refresh-abc", "ghp_testtoken")


@pytest.fixture
def world(settings, db, secrets_memory, monkeypatch):
    policy = install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, version = make_plan(db, project)
    office = make_project(db, domain="OFFICE", name="Kantor")
    oplan, _ov = make_plan(db, office)
    return {"db": db, "policy": policy, "fakes": fakes, "project": project, "plan": plan, "version": version,
            "office": office, "office_plan": oplan}


def _no_secrets(text: str) -> None:
    for m in SECRET_MARKERS:
        assert m not in text


def _preview(client, world):
    r = client.post(f"/api/plans/{world['plan'].id}/publications/preview", json={"render_visuals": False})
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(view):
    return {"plan_version_id": view["plan_version_id"], "approved_hash": view["approved_hash"],
            "preview_hash": view["preview_hash"]}


def test_preview_execute_and_view(personal_client, world):
    view = _preview(personal_client, world)
    assert view["status"] == "PREVIEW" and view["operations"] and view["preview_hash"]
    assert set(view["status_dimensions"]) == {"APPROVED", "IMPLEMENTED", "ACTIVE", "VERIFIED",
                                              "ARCHITECTURE_SOURCE_UPDATED", "DOCS_PUBLISHED"}
    assert "Satu persetujuan" in view["approval_statement"]
    r = personal_client.post(f"/api/publications/{view['id']}/execute", json=_confirm(view))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RUNNING"
    jobs.process_publication(world["db"], view["id"])  # what the background worker does
    detail = personal_client.get(f"/api/publications/{view['id']}").json()
    assert detail["status"] == "PUBLISHED" and detail["status_dimensions"]["DOCS_PUBLISHED"]["status"] == "PUBLISHED"
    assert detail["status_dimensions"]["ACTIVE"]["status"] == "UNKNOWN"  # separate from DOCS_PUBLISHED
    op = detail["operations"][0]
    assert {"status", "external_url", "error", "attempts", "idempotency_key", "depends_on"} <= set(op)
    listing = personal_client.get(f"/api/publications?project_id={world['project'].id}").json()
    assert listing["items"][0]["id"] == view["id"] and {"source", "checked_at", "mode"} <= set(listing)
    _no_secrets(str(detail) + str(listing))


def test_execute_denied_by_interlock_is_403_and_audited(personal_client, world):
    world["policy"].write_enabled = False
    view = _preview(personal_client, world)
    r = personal_client.post(f"/api/publications/{view['id']}/execute", json=_confirm(view))
    assert r.status_code == 403 and r.json()["error"]["code"] == "INTERLOCK_DENIED"
    assert r.json()["error"]["details"]["reasons"]
    db = world["db"]
    db.expire_all()
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "publication.execute",
                                              AuditEvent.result == "DENIED")) is not None
    assert all(not f.calls for f in world["fakes"].values())


def test_execute_with_stale_confirmation_is_409(personal_client, world):
    view = _preview(personal_client, world)
    body = _confirm(view) | {"preview_hash": "sha256:stale"}
    r = personal_client.post(f"/api/publications/{view['id']}/execute", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_PREVIEW"


def test_viewer_cannot_preview_execute_or_sync(client, db, world):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    for url in (f"/api/plans/{world['plan'].id}/publications/preview", "/api/publications/x/execute",
                "/api/publications/x/retry", "/api/publications/x/reconcile",
                f"/api/projects/{world['project'].id}/work-items/sync",
                f"/api/projects/{world['project'].id}/documents/render"):
        r = client.post(url, json={})
        assert r.status_code == 403, url
        assert r.json()["error"]["code"] == "READ_ONLY_ROLE"
    # reads are allowed
    assert client.get(f"/api/projects/{world['project'].id}/documents").status_code == 200


def test_cross_domain_is_404(personal_client, world):
    r = personal_client.post(f"/api/plans/{world['office_plan'].id}/publications/preview", json={})
    assert r.status_code == 404
    for url in (f"/api/projects/{world['office'].id}/documents", f"/api/projects/{world['office'].id}/work-items",
                f"/api/projects/{world['office'].id}/publication-targets",
                f"/api/publications?project_id={world['office'].id}"):
        assert personal_client.get(url).status_code == 404, url


def test_domain_must_be_selected(admin_client, world):
    r = admin_client.get(f"/api/projects/{world['project'].id}/documents")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_partial_publication_retry_and_reconcile_endpoints(personal_client, world):
    from aeos.publisher.providers.base import ProviderError

    world["fakes"]["github-issues"].fail["issue:task:T1"] = ProviderError("boom", retryable=False)
    view = _preview(personal_client, world)
    personal_client.post(f"/api/publications/{view['id']}/execute", json=_confirm(view))
    jobs.process_publication(world["db"], view["id"])
    detail = personal_client.get(f"/api/publications/{view['id']}").json()
    assert detail["status"] == "PARTIAL" and detail["attention"]
    rec = personal_client.post(f"/api/publications/{view['id']}/reconcile")
    assert rec.status_code == 200 and "found" in rec.json()["result"]
    rt = personal_client.post(f"/api/publications/{view['id']}/retry", json={})
    assert rt.status_code == 200 and rt.json()["status"] == "RUNNING"
    jobs.process_publication(world["db"], view["id"])
    assert personal_client.get(f"/api/publications/{view['id']}").json()["status"] == "PUBLISHED"


def test_work_items_projection_endpoints(personal_client, world, monkeypatch):
    class TrackerFake(FakeProvider):
        def issue_state(self, number):
            return {"external_status": "closed:completed", "url": f"https://github.com/owner/contoh/issues/{number}"}

    from aeos.publisher import providers

    tracker = TrackerFake()
    monkeypatch.setitem(providers._overrides, "github-issues", lambda _cfg: tracker)
    db = world["db"]
    db.add(OperationalWorkProjection(project_id=world["project"].id, provider="github-issues", issue_id="4",
                                     external_status="open"))
    db.commit()
    r = personal_client.get(f"/api/projects/{world['project'].id}/work-items")
    assert r.status_code == 200 and r.json()["authority"] == "PROJECTION_ONLY" and r.json()["items"]
    s = personal_client.post(f"/api/projects/{world['project'].id}/work-items/sync")
    assert s.status_code == 200, s.text
    item = s.json()["items"][0]
    assert item["external_status"] == "closed:completed" and item["unlocks_runtime_dependencies"] is False
    assert item["last_synced_at"]


def test_connections_and_targets_show_status_without_values(personal_client, world):
    c = personal_client.get("/api/publisher/connections").json()
    assert c["google"]["status"] == "CONFIGURED" and c["github"]["status"] == "CONFIGURED"
    t = personal_client.get(f"/api/projects/{world['project'].id}/publication-targets").json()
    assert {i["family"] for i in t["items"]} == {"google", "github-issues"}
    _no_secrets(str(c) + str(t))
    cand = personal_client.get(f"/api/projects/{world['project'].id}/publication-candidates").json()
    assert cand["items"][0]["approved"]["plan_version_id"] == world["version"].id


def test_documents_endpoints(personal_client, world):
    pid = world["project"].id
    empty = personal_client.get(f"/api/projects/{pid}/documents").json()
    assert empty["items"] == [] and "aeos docs render" in empty["remedy"]
    r = personal_client.post(f"/api/projects/{pid}/documents/render",
                             json={"plan_version_id": world["version"].id, "render_visuals": False})
    assert r.status_code == 200 and r.json()["delta"]["created"]
    docs = personal_client.get(f"/api/projects/{pid}/documents").json()
    assert docs["product_title"].startswith("PRODUCT 1 (") and docs["summary"]["documents"] > 20
    item = next(i for i in docs["items"] if i["logical_doc_id"] == "02-requirements/prd-tasks")
    assert {"status", "content_type", "pending_sections", "freshness", "external", "sections"} <= set(item)
    md = personal_client.get(f"/api/documents/{item['id']}/render").json()
    assert md["markdown"].startswith("# PRD") and md["format"] == "markdown"
    st = personal_client.get("/api/docs/product1-structure").json()
    assert st["mode"] == "DESIGN" and len(st["templates"]) == 12
    assert personal_client.get("/api/docs/visuals/status").status_code == 200


def test_render_endpoint_refuses_unapproved_version(personal_client, world):
    from tests.unit.test_publisher_support import add_version

    db = world["db"]
    v2 = add_version(db, world["plan"], {"summary": "draft"})
    db.commit()
    r = personal_client.post(f"/api/projects/{world['project'].id}/documents/render",
                             json={"plan_version_id": v2.id, "render_visuals": False})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_NOT_APPROVED"


def test_acknowledge_human_edit_revision_input(personal_client, world):
    db = world["db"]
    pid = world["project"].id
    personal_client.post(f"/api/projects/{pid}/documents/render",
                         json={"plan_version_id": world["version"].id, "render_visuals": False})
    db.expire_all()
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.project_id == pid,
                                                 DocumentRecord.logical_doc_id == "00-product-home"))
    rev = DocumentRevision(document_id=rec.id, project_id=pid, domain="PERSONAL", logical_doc_id=rec.logical_doc_id,
                           kind="HUMAN_EDIT_INPUT", status="OPEN", observed_revision="rev-h", expected_revision="rev-1",
                           body_markdown="edit manusia")
    db.add(rev)
    db.commit()
    revs = personal_client.get(f"/api/documents/{rec.id}/revisions").json()["items"]
    assert any(x["kind"] == "HUMAN_EDIT_INPUT" and x["excerpt"] == "edit manusia" for x in revs)
    docs = personal_client.get(f"/api/projects/{pid}/documents").json()
    assert next(i for i in docs["items"] if i["id"] == rec.id)["open_revision_inputs"] == 1
    ok = personal_client.post(f"/api/documents/{rec.id}/revision-inputs/{rev.id}/acknowledge", json={"note": "ok"})
    assert ok.status_code == 200 and ok.json()["status"] == "ACKNOWLEDGED"
    again = personal_client.post(f"/api/documents/{rec.id}/revision-inputs/{rev.id}/acknowledge", json={})
    assert again.status_code == 409


def test_document_diff_and_live_evidence_references(personal_client, world):
    import copy

    from aeos.core.hashing import content_hash
    from aeos.models.evidence import EvidenceArtifact
    from aeos.models.planning import Plan, TaskSpec
    from tests.unit.test_publisher_support import SAMPLE_CONTENT, add_version, approve

    db = world["db"]
    pid = world["project"].id
    personal_client.post(f"/api/projects/{pid}/documents/render",
                         json={"plan_version_id": world["version"].id, "render_visuals": False})
    plan = db.get(Plan, world["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"][0]["acceptance"].append("User dapat mencari tugas")
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    personal_client.post(f"/api/projects/{pid}/documents/render", json={"plan_version_id": v2.id,
                                                                        "render_visuals": False})
    db.expire_all()
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.project_id == pid,
                                                 DocumentRecord.logical_doc_id == "02-requirements/prd-tasks"))
    d = personal_client.get(f"/api/documents/{rec.id}/diff").json()
    assert d["changed"] and "+" in d["diff"] and "User dapat mencari tugas" in d["diff"]
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v2.id, TaskSpec.task_key == "T1"))
    db.add(EvidenceArtifact(project_id=pid, domain="PERSONAL", task_id=task.id, kind="TEST_REPORT", origin="verifier",
                            collector_identity="verifier", trusted=True, content_hash=content_hash("x"),
                            evidence_level="E2"))
    db.commit()
    docs = personal_client.get(f"/api/projects/{pid}/documents").json()
    item = next(i for i in docs["items"] if i["id"] == rec.id)
    assert item["evidence"] and item["evidence"][0]["trusted"] is True and item["evidence"][0]["evidence_level"] == "E2"


# ------------------------------------------------------------------------------------------- CLI


def _ascii(text: str) -> str:
    # Rich/typer draw help panels with box characters (framework, not our strings); everything we print is ASCII.
    ours = "".join(ch for ch in text if not 0x2500 <= ord(ch) <= 0x257F)
    ours.encode("ascii")  # raises if not ASCII
    return text


def test_cli_docs_structure_and_publish_help_are_ascii():
    from aeos.cli import app

    runner = CliRunner()
    out = runner.invoke(app, ["docs", "structure"])
    assert out.exit_code == 0, out.output
    text = _ascii(out.output)
    assert "[doc] 00. Product Home" in text and "Test Strategy [AI_MAINTAINED]" in text
    assert "PRODUCT 1 (<Nama Produk>) - SOFTWARE DEVELOPMENT & ENGINEERING" in text
    gh = runner.invoke(app, ["publish", "github-connect"])
    assert gh.exit_code == 0 and "gh auth token | aeos secrets set personal/github --stdin" in _ascii(gh.output)
    for cmd in (["publish", "--help"], ["docs", "--help"], ["publish", "google-connect", "--help"]):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 0
        _ascii(res.output)


def test_cli_google_status_without_store_shows_remedy(settings):
    from aeos.cli import app

    res = CliRunner().invoke(app, ["publish", "google-status"])
    assert res.exit_code == 0 and "BLOCKED" in res.output and "google-connect" in _ascii(res.output)


def test_cli_docs_render_local_preview(settings, db, monkeypatch, tmp_path):
    install_fakes(monkeypatch)
    project = make_project(db)
    _plan, version = make_plan(db, project)
    from aeos.cli import app

    res = CliRunner().invoke(app, ["docs", "render", version.id, "--out", str(tmp_path / "prev"), "--no-visuals"])
    assert res.exit_code == 0, res.output
    text = _ascii(res.output)
    assert "Approved version: yes" in text and "Preview written to" in text
    assert (tmp_path / "prev" / "INDEX.md").is_file() and (tmp_path / "prev" / "00-product-home.md").is_file()


def test_owner_can_reject_a_preview_without_any_external_write(personal_client, world):
    """§05 step 9: Approve & Publish, Request Revision or Reject — reject writes nothing and is audited."""
    view = _preview(personal_client, world)
    r = personal_client.post(f"/api/publications/{view['id']}/reject", json={"reason": "target salah"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED" and "ditolak" in r.json()["status_reason"]
    assert all(not f.calls for f in world["fakes"].values())
    again = personal_client.post(f"/api/publications/{view['id']}/execute", json=_confirm(view))
    assert again.status_code == 409 and again.json()["error"]["code"] == "NOT_A_PREVIEW"
    assert personal_client.post(f"/api/publications/{view['id']}/reject", json={}).status_code == 409
    db = world["db"]
    db.expire_all()
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "publication.reject")) is not None
    # a new preview adopts the released outbox rows (one row per side effect)
    view2 = _preview(personal_client, world)
    assert view2["status"] == "PREVIEW" and len(view2["operations"]) == len(view["operations"])


def test_viewer_cannot_reject_or_review(client, db, world):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    for url in ("/api/publications/x/reject", "/api/documents/x/review"):
        r = client.post(url, json={"content_hash": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE", url
