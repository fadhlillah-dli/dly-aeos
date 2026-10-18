"""Publication (§14, §26, §29, §41). §50 rows:

* 1.15 "Bootstrap belum selesai tetapi publish diminta" -> interlock refuses;
* 1.16 "Approval PlanVersion lama" -> refused; the latest revision is reported;
* 1.17 "Revisi baru muncul selama publish" -> the job keeps the approved version or stops; never "latest";
* 1.18 "Google Docs direvisi manusia" -> requiredRevisionId conflict; no overwrite;
* 1.19 "Tenant tidak mendukung folder Confluence yang wajib" -> block/needs action, structure unchanged;
* 1.20 "Provider timeout setelah create resource" -> reconcile ID/target before any retry;
* 1.21 "Salah satu target publikasi gagal" -> PARTIAL, successes recorded, nothing re-created;
* 3.23 "Runtime aktif tetapi external docs publication conflict" -> ACTIVE/VERIFIED separate from
  DOCS_PUBLISHED; PARTIAL/CONFLICT visible;
* 3.24 "GDocs/Confluence diedit manusia selama update" -> conflict respected, revision input to review.

Google Drive/Docs, GitHub and Confluence are faked with ``respx`` (stateful fakes in ``fakes.py``);
planning, policy (decision registry + external-write interlock), docs engine and the publisher run for
real, end to end, in the one provider vocabulary (``aeos.portfolio.providers``: Google Docs are
``google-drive`` operations, targets are ``{provider, root_id}``); the adapter-level contracts are also
proven separately.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select
from typer.testing import CliRunner

from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.publishing import DocumentRevision, ExternalReference
from aeos.policy import facade as policy
from aeos.publisher.providers.base import OpContext, ProviderBlocked, ProviderConflict
from tests.acceptance.fakes import FakeClaude, FakeGitHubApi, FakeGoogleApi, google_token_json
from tests.acceptance.world import (
    PUB_FOLDER,
    PUB_REPO,
    PlanningResponder,
    approve_current,
    create_project,
    plan_to_review,
    preview_and_execute,
    publication_ready,
    run_outbox,
)
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

# The end-to-end rows below were strict xfails (BUG_BINDING: decision kind/target binding and provider
# vocabulary mismatches made every real publication INTERLOCK_DENIED) until the W1 planning/publication seam.


def _google_op(operation: str, logical_id: str, *, title: str = "Product Home", payload: dict | None = None,
               existing_id: str | None = None, existing_rev: str | None = None,
               attachments: dict | None = None) -> OpContext:
    return OpContext(operation_id="op-1", publication_id="pub-1", project_id="proj-1", project_key="pub-app",
                     project_name="Pub App", domain="PERSONAL", provider="google-drive", operation=operation,
                     logical_id=logical_id, idempotency_key="sha256:" + "a" * 64, title=title, target={},
                     payload=payload or {}, existing_external_id=existing_id, existing_revision=existing_rev,
                     attachments=attachments or {})


# ---------------------------------------------------------------------------------------------- 1.15
def test_publish_before_bootstrap_is_refused(client: AuthedClient, db, settings, host, vault):
    assert client.get("/api/auth/bootstrap-status").json() == {"bootstrap_complete": False}
    for url in ("/api/plans/x/publications/preview", "/api/publications/x/execute", "/api/publications/x/retry"):
        assert client.client.post(url, json={}).status_code in (401, 403), url
    intent = policy.WriteIntent(actor_user_id="nobody", project_id="p", domain="PERSONAL", provider="google-drive",
                                operation="create_doc", target={"provider": "google-drive", "root_id": "F"},
                                approved_plan_version_id="v", approved_hash="sha256:x", decision_id="d",
                                capability_expires_at=utcnow() + timedelta(minutes=5))
    verdict = policy.check_external_write(db, intent)
    db.commit()
    assert verdict.allowed is False
    assert verdict.reasons[0].startswith("BOOTSTRAP_INCOMPLETE")
    denied = db.scalar(select(AuditEvent).where(AuditEvent.action == "policy.interlock.check",
                                                AuditEvent.result == "DENIED"))
    assert denied is not None and "BOOTSTRAP_INCOMPLETE" in denied.details["reason_codes"]
    from aeos.cli import app

    out = json.loads(CliRunner().invoke(app, ["doctor", "--json", "--capability", "CAN_PUBLISH_PERSONAL"]).output)
    by_id = {r["check_id"]: r for r in out["results"]}
    assert by_id["auth.bootstrap"]["status"] != "VERIFIED"
    cap = next(c for c in out["capabilities"] if c["capability"] == "CAN_PUBLISH_PERSONAL")
    assert cap["status"] != "READY"


# ---------------------------------------------------------------------------------------------- 1.16
def test_approving_an_old_plan_version_is_refused_and_latest_reported(personal_client: AuthedClient, db, monkeypatch):
    FakeClaude(monkeypatch, PlanningResponder())
    project = create_project(personal_client, name="Versioned")
    detail = plan_to_review(personal_client, project["id"])
    v1, v2 = detail["versions"]
    stale = personal_client.post(f"/api/plans/{detail['id']}/decision", json={
        "action": "APPROVE", "version_id": v1["id"], "version_hash": v1["manifest_hash"]})
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "STALE_PLAN_VERSION" and err["details"]["latest_version_id"] == v2["id"]
    wrong_hash = personal_client.post(f"/api/plans/{detail['id']}/decision", json={
        "action": "APPROVE", "version_id": v2["id"], "version_hash": v1["manifest_hash"]})
    assert wrong_hash.status_code == 409 and wrong_hash.json()["error"]["code"] == "STALE_PLAN_VERSION"
    plan = personal_client.get(f"/api/plans/{detail['id']}").json()
    assert plan["current_version"]["id"] == v2["id"] and plan["approved_version"] is None
    assert plan["decisions"] == []  # nothing was recorded for the stale attempts
    db.expire_all()
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "plan.decision",
                                                 AuditEvent.result == "DENIED")).all()
    assert sorted(a.target_id for a in denied) == sorted([v1["id"], v2["id"]]), \
        "each refused stale approval stays in the audit trail although the request failed"
    approve_current(personal_client, detail["id"])
    # a human edit creates v3: the approval of v2 no longer authorises anything
    content = personal_client.get(f"/api/plans/{detail['id']}/versions/{v2['id']}").json()["content"]
    content["overview"]["overview"] = "Diubah manusia"
    r = personal_client.post(f"/api/plans/{detail['id']}/versions",
                             json={"parent_version_id": v2["id"], "content": content, "rationale": "perbaiki ringkasan"})
    assert r.status_code == 201, r.text
    after = personal_client.get(f"/api/plans/{detail['id']}").json()
    assert after["approved_version"] is None and after["current_version"]["id"] == r.json()["id"]
    assert personal_client.post(f"/api/plans/{detail['id']}/decision", json={
        "action": "APPROVE", "version_id": v2["id"], "version_hash": v2["manifest_hash"]}).status_code == 409


# ---------------------------------------------------------------------------------------------- 1.17
def test_publication_binds_the_approved_version_never_latest(personal_client: AuthedClient, db, vault, monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    plan = world["plan"]
    approved = plan["approved_version"]
    pv = personal_client.post(f"/api/plans/{plan['id']}/publications/preview", json={"render_visuals": False})
    assert pv.status_code == 200, pv.text
    pub = pv.json()
    assert pub["plan_version_id"] == approved["id"] and pub["approved_hash"] == approved["manifest_hash"]
    # a new revision appears (human edit) after the preview was taken
    content = personal_client.get(f"/api/plans/{plan['id']}/versions/{approved['id']}").json()["content"]
    content["overview"]["overview"] = "Revisi baru saat publikasi"
    r = personal_client.post(f"/api/plans/{plan['id']}/versions", json={
        "parent_version_id": approved["id"], "content": content, "rationale": "revisi baru"})
    assert r.status_code == 201, r.text
    # the bound preview stops (contract) instead of publishing the newer, unapproved content
    ex = personal_client.post(f"/api/publications/{pub['id']}/execute", json={
        "plan_version_id": pub["plan_version_id"], "approved_hash": pub["approved_hash"],
        "preview_hash": pub["preview_hash"]})
    assert ex.status_code in (409, 403) and ex.json()["error"]["code"] in ("PLAN_NOT_APPROVED", "APPROVAL_STALE")
    assert personal_client.post(f"/api/plans/{plan['id']}/publications/preview",
                                json={"render_visuals": False}).json()["error"]["code"] == "PLAN_NOT_APPROVED"
    cands = personal_client.get(f"/api/projects/{world['project']['id']}/publication-candidates").json()["items"]
    assert cands[0]["approved"] is None and cands[0]["approval_status"] == "NOT_APPROVED"
    assert db.scalar(select(func.count()).select_from(ExternalReference)) == 0  # nothing written anywhere


# formerly strict xfail BUG_BINDING, fixed by seam W1
def test_new_revision_during_a_running_publication_keeps_the_approved_snapshot(personal_client: AuthedClient, db,
                                                                               vault, monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    plan = world["plan"]
    approved = plan["approved_version"]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        google = FakeGoogleApi().mount(router)
        FakeGitHubApi(PUB_REPO).mount(router)
        pub = preview_and_execute(personal_client, plan["id"])
        from aeos.publisher import jobs

        jobs.process_publication(db, pub["id"], max_ops=3)  # partway through
        content = personal_client.get(f"/api/plans/{plan['id']}/versions/{approved['id']}").json()["content"]
        content["overview"]["overview"] = "REVISI-SAAT-PUBLISH"
        r = personal_client.post(f"/api/plans/{plan['id']}/versions", json={
            "parent_version_id": approved["id"], "content": content, "rationale": "revisi saat publish"})
        assert r.status_code == 409  # the contract: no new revision replaces the approved one mid-publication
        view = run_outbox(db, pub["id"])
    assert view["plan_version_id"] == approved["id"] and view["status"] == "PUBLISHED"
    assert google.files and all("REVISI-SAAT-PUBLISH" not in f["text"] for f in google.files.values())
    assert personal_client.get(f"/api/plans/{plan['id']}").json()["state"] == "PUBLISHED"


# ---------------------------------------------------------------------------------------------- 1.18 / 3.24
def test_google_docs_human_revision_is_a_conflict_never_an_overwrite(settings, vault):
    from aeos.docs_engine.docx import markdown_to_docx
    from aeos.publisher.providers.google import GoogleAuth, GoogleDriveDocsProvider

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        google = FakeGoogleApi().mount(router)
        client = httpx.Client()
        provider = GoogleDriveDocsProvider(client=client, auth=GoogleAuth(client, token_json=google_token_json()))
        docx = markdown_to_docx("# Product Home\n\nIsi awal.\n", title="Product Home", logical_id="00-product-home")
        created = provider.execute(_google_op("create_doc", "00-product-home", attachments={"document.docx": docx}))
        assert created.external_revision == "r1"
        google.human_edit("00-product-home", "Catatan manusia yang tidak boleh hilang\n")
        writes_before = list(google.writes)
        with pytest.raises(ProviderConflict) as exc:
            provider.execute(_google_op("update_doc", "00-product-home", payload={"markdown": "# Baru\n\nTimpa?\n"},
                                        existing_id=created.external_id, existing_rev=created.external_revision))
        assert google.files[created.external_id]["text"] == "Catatan manusia yang tidak boleh hilang\n"
        assert google.writes == writes_before, "nothing was written after the revision mismatch"
        assert exc.value.expected_revision == "r1" and exc.value.observed_revision == "r2"
        assert "Catatan manusia" in exc.value.current_text  # the human edit becomes revision input
        # with the current revision the update applies under writeControl.requiredRevisionId
        ok = provider.execute(_google_op("update_doc", "00-product-home", payload={"markdown": "# Baru\n\nIsi.\n"},
                                         existing_id=created.external_id, existing_rev="r2"))
        assert ok.external_revision == "r3"
        client.close()


# formerly strict xfail BUG_BINDING, fixed by seam W1
def test_human_edit_during_update_is_kept_and_handed_to_review(personal_client: AuthedClient, db, vault, monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    plan = world["plan"]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        google = FakeGoogleApi().mount(router)
        FakeGitHubApi(PUB_REPO).mount(router)
        pub = preview_and_execute(personal_client, plan["id"])
        assert run_outbox(db, pub["id"])["status"] == "PUBLISHED"
        google.human_edit("01-product-overview", "Diedit manusia di Google Docs\n")
        approved = plan["approved_version"]
        content = personal_client.get(f"/api/plans/{plan['id']}/versions/{approved['id']}").json()["content"]
        content["overview"]["overview"] = "Ringkasan produk versi baru"
        r = personal_client.post(f"/api/plans/{plan['id']}/versions", json={
            "parent_version_id": approved["id"], "content": content, "rationale": "update ringkasan"})
        assert r.status_code == 201, r.text
        approve_current(personal_client, plan["id"], action="APPROVE_AND_PUBLISH",
                        targets=[{"provider": "google-drive", "root_id": PUB_FOLDER}])
        second = preview_and_execute(personal_client, plan["id"], targets=["docs"])
        view = run_outbox(db, second["id"])
    overview = next(o for o in view["operations"] if o["logical_id"] == "01-product-overview")
    assert overview["status"] == "CONFLICT"
    assert view["status"] in ("PARTIAL", "CONFLICT") and view["status_dimensions"]["DOCS_PUBLISHED"]["status"] in (
        "PARTIAL", "CONFLICT")
    assert google.by_logical("01-product-overview")[0]["text"] == "Diedit manusia di Google Docs\n"
    rev = db.scalar(select(DocumentRevision).where(DocumentRevision.kind == "HUMAN_EDIT_INPUT"))
    assert rev is not None and rev.status == "OPEN" and "Diedit manusia" in rev.body_markdown
    revisions = personal_client.get(f"/api/documents/{rev.document_id}/revisions").json()
    assert any(i.get("kind") == "HUMAN_EDIT_INPUT" and i.get("status") == "OPEN"
               for i in revisions.get("items", revisions.get("revisions", [])))


# ---------------------------------------------------------------------------------------------- 1.19
def test_confluence_parent_without_folder_support_blocks_without_changing_structure(admin_client: AuthedClient):
    from aeos.core.enums import Domain
    from aeos.publisher.providers.atlassian import AtlassianConfig, ConfluenceProvider, readiness

    base = "https://corp.atlassian.net"
    cfg = AtlassianConfig(base_url=base, enabled=True, deployment="CLOUD", space_id="S1", parent_id="12345")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(f"{base}/wiki/api/v2/spaces/S1").respond(200, json={"id": "S1"})
        router.get(f"{base}/wiki/api/v2/folders/12345").respond(400, json={"message": "folders not supported"})
        router.get(f"{base}/wiki/api/v2/pages/12345").respond(200, json={"id": "12345", "title": "Projects"})
        create_folder = router.post(f"{base}/wiki/api/v2/folders").respond(200, json={"id": "x"})
        create_page = router.post(f"{base}/wiki/api/v2/pages").respond(200, json={"id": "y"})
        client = httpx.Client()
        provider = ConfluenceProvider(cfg, client=client, auth_header="Basic fake")
        caps = provider.check_capabilities()
        assert caps["status"] == "BLOCKED" and caps["folders"] == "NOT_SUPPORTED"
        assert any("needs action" in r for r in caps["reasons"])
        with pytest.raises(ProviderBlocked) as exc:
            provider.execute(OpContext(
                operation_id="o", publication_id="p", project_id="proj", project_key="svc", project_name="Svc",
                domain="OFFICE", provider="confluence", operation="create_folder", logical_id="root", idempotency_key="k",
                title="Svc", target={}, payload={"title": "Svc"}))
        assert exc.value.code == "CONFLUENCE_FOLDERS_UNSUPPORTED"
        assert not create_folder.called and not create_page.called, "structure is never silently changed to pages"
        client.close()
    assert readiness(AtlassianConfig(base_url=base, enabled=True, deployment="DATA_CENTER", space_id="S1",
                                     parent_id="1"), product="confluence")[0] == "NOT_SUPPORTED"
    # OFFICE targets stay blocked by default in the integrated system (no company authorisation)
    admin_client.select_domain(Domain.OFFICE)
    office = create_project(admin_client, domain="OFFICE", name="Office Docs")
    targets = admin_client.get(f"/api/projects/{office['id']}/publication-targets").json()["items"]
    assert {t["family"] for t in targets} == {"confluence", "jira"}
    assert all(t["status"] != "READY" for t in targets)


@pytest.mark.integration
def test_confluence_folder_support_on_a_real_tenant():
    pytest.skip("BLOCKED-real-atlassian-tenant: folder support of a real Confluence Cloud tenant/parent needs an "
                "authorised OFFICE tenant (master §51 P2); only respx-mocked behaviour is proven here")


# ---------------------------------------------------------------------------------------------- 1.20
# formerly strict xfail BUG_BINDING, fixed by seam W1
def test_provider_timeout_after_create_is_reconciled_before_retry(personal_client: AuthedClient, db, vault,
                                                                  monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        google = FakeGoogleApi().mount(router)
        FakeGitHubApi(PUB_REPO).mount(router)
        google.timeout_after_create.add("00-product-home")
        pub = preview_and_execute(personal_client, world["plan"]["id"])
        from aeos.publisher import jobs

        jobs.process_publication(db, pub["id"])
        db.expire_all()
        op = next(o for o in jobs.publication_view(db, db.get(jobs.Publication, pub["id"]))["operations"]
                  if o["logical_id"] == "00-product-home")
        assert op["status"] == "UNKNOWN"
        jobs.process_publication(db, pub["id"], now=utcnow() + timedelta(hours=2))
        view = run_outbox(db, pub["id"])
    home = next(o for o in view["operations"] if o["logical_id"] == "00-product-home")
    assert home["status"] == "SUCCEEDED" and home["reconciled_at"]
    assert len(google.by_logical("00-product-home")) == 1, "reconciled by marker - no duplicate document"


# ---------------------------------------------------------------------------------------------- 1.21 / 3.23
# formerly strict xfail BUG_BINDING, fixed by seam W1
def test_one_failing_target_is_partial_and_successes_are_kept(personal_client: AuthedClient, db, vault, monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        google = FakeGoogleApi().mount(router)
        github = FakeGitHubApi(PUB_REPO).mount(router)
        github.fail_create_issue["Task T1"] = 422
        pub = preview_and_execute(personal_client, world["plan"]["id"])
        view = run_outbox(db, pub["id"])
        created_docs = len(google.files)
        assert view["status"] == "PARTIAL"
        dims = view["status_dimensions"]
        assert dims["DOCS_PUBLISHED"]["status"] == "PARTIAL"
        assert dims["VERIFIED"]["status"] != "VERIFIED" and dims["ACTIVE"]["status"] == "UNKNOWN"
        refs = db.scalar(select(func.count()).select_from(ExternalReference))
        assert refs >= created_docs  # every success is recorded
        github.fail_create_issue.clear()
        r = personal_client.post(f"/api/publications/{pub['id']}/retry", json={})
        assert r.status_code == 200, r.text
        retried = run_outbox(db, pub["id"])
        assert len(google.files) == created_docs, "the retry never re-creates what already succeeded"
        assert retried["status"] == "PUBLISHED"


def test_publication_status_keeps_docs_published_separate_from_active_and_verified(
        personal_client: AuthedClient, db, vault, monkeypatch):
    world = publication_ready(personal_client, db, vault, monkeypatch)
    pv = personal_client.post(f"/api/plans/{world['plan']['id']}/publications/preview", json={"render_visuals": False})
    assert pv.status_code == 200, pv.text
    view = personal_client.get(f"/api/publications/{pv.json()['id']}").json()
    dims = view["status_dimensions"]
    assert set(dims) == {"APPROVED", "IMPLEMENTED", "VERIFIED", "ACTIVE", "ARCHITECTURE_SOURCE_UPDATED",
                         "DOCS_PUBLISHED"}, "separate fields, never one green badge"
    assert dims["APPROVED"]["status"] == "APPROVED" and dims["DOCS_PUBLISHED"]["status"] == "NOT_PUBLISHED"
    assert dims["ACTIVE"]["status"] == "UNKNOWN" and dims["VERIFIED"]["status"] != "VERIFIED"
    assert {d["source"] for d in dims.values()} >= {"publisher", "operations", "verification"}
