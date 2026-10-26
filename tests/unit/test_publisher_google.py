"""Google Drive/Docs adapter (S16-S21) with mocked HTTP (respx) — never real credentials or network.

§50 rows: "Google Docs direvisi manusia → requiredRevisionId conflict; tidak overwrite",
"Provider timeout setelah create resource → Rekonsiliasi ID/target sebelum retry".
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.publishing import DocumentRecord, DocumentRevision, ExternalReference
from aeos.publisher import jobs
from aeos.publisher.providers import google as gg
from aeos.publisher.providers.base import OpContext, ProviderConflict
from tests.unit.test_publisher_support import (
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
)


def _token() -> str:
    """A stored OAuth token that is valid when the test uses it (computed then, never at import: a suite running
    longer than the token lifetime must not see it expire)."""
    return json.dumps({"token": "ya29.unit-test-access-token-0123456789",
                       "refresh_token": "1//unit-refresh-0123456789abc", "client_id": "cid",
                       "client_secret": "csecret-value", "token_uri": gg.DEFAULT_TOKEN_URI,
                       "scopes": [gg.DRIVE_FILE_SCOPE], "expiry": (utcnow() + timedelta(hours=1)).isoformat()})


def _ctx(**kw) -> OpContext:
    base = dict(operation_id="op", publication_id="pub", project_id="proj", project_key="k", project_name="Contoh",
                domain="PERSONAL", provider="google-drive", operation="create_folder", logical_id="root",
                idempotency_key="sha256:" + "1" * 64, title="Contoh", target={}, payload={})
    base.update(kw)
    return OpContext(**base)


def _provider(token: str | None = None, root: str | None = None) -> gg.GoogleDriveDocsProvider:
    client = httpx.Client()
    return gg.GoogleDriveDocsProvider(client=client, auth=gg.GoogleAuth(client, token_json=token or _token()),
                                      root_folder_id=root)


# ---------------------------------------------------------------------------------------------- OAuth


def test_only_drive_file_scope_is_ever_requested(tmp_path, secrets_memory):
    assert gg.SCOPES == ("https://www.googleapis.com/auth/drive.file",)
    src = Path(gg.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r"https://www\.googleapis\.com/auth/[a-z.]+", src)) == {gg.DRIVE_FILE_SCOPE}
    client_file = tmp_path / "client_secret.json"
    client_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs-123456",
                                                     "redirect_uris": ["http://localhost"]}}), encoding="utf-8")
    seen: dict = {}

    class Creds:
        def to_json(self):
            return _token()

    class Flow:
        def run_local_server(self, **kw):
            seen["run"] = kw
            return Creds()

    def factory(config, scopes):
        seen["scopes"] = scopes
        return Flow()

    out = gg.connect(client_file, actor="test", open_browser=False, flow_factory=factory)
    assert seen["scopes"] == [gg.DRIVE_FILE_SCOPE]
    assert seen["run"]["host"] == "127.0.0.1" and seen["run"]["port"] == 0  # loopback, free port
    assert out["least_privilege"] is True and out["granted_scopes"] == [gg.DRIVE_FILE_SCOPE]
    assert secrets_memory.status(gg.CLIENT_REF).exists and secrets_memory.status(gg.TOKEN_REF).exists
    assert "ya29" not in json.dumps(out) and "cs-123456" not in json.dumps(out)


def test_web_oauth_clients_are_rejected(tmp_path):
    from aeos.core.errors import ValidationFailed

    f = tmp_path / "web.json"
    f.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
    with pytest.raises(ValidationFailed):
        gg.load_client_config(f)


def test_google_status_never_returns_secret_values(secrets_memory):
    seed_personal_secrets(secrets_memory)
    st = gg.google_status()
    dumped = json.dumps(st)
    assert st["status"] == "CONFIGURED" and st["granted_scopes"] == [gg.DRIVE_FILE_SCOPE]
    assert "ya29" not in dumped and "refresh" not in dumped.replace("has_refresh_token", "")


def test_google_status_without_store_is_blocked():
    st = gg.google_status()  # no secret provider configured in this test
    assert st["status"] == "BLOCKED" and "remedy" in st


def test_the_test_token_is_valid_whenever_a_test_uses_it(monkeypatch):
    """Test hygiene: the token expiry is computed when a test uses it, not when the module is imported - a suite
    running longer than the token lifetime (one hour) must not hand later tests an expired token."""
    from datetime import datetime

    later = utcnow() + timedelta(hours=3)
    monkeypatch.setattr(f"{__name__}.utcnow", lambda: later)
    assert datetime.fromisoformat(json.loads(_token())["expiry"]) > later


@respx.mock
def test_expired_token_is_refreshed_in_process(secrets_memory):
    expired = json.loads(_token()) | {"expiry": (utcnow() - timedelta(minutes=5)).isoformat()}
    route = respx.post(gg.DEFAULT_TOKEN_URI).mock(return_value=httpx.Response(200, json={
        "access_token": "ya29.refreshed-access-token-abcdefghij", "expires_in": 3600, "scope": gg.DRIVE_FILE_SCOPE}))
    create = respx.post(f"{gg.DRIVE_API}/files").mock(return_value=httpx.Response(200, json={"id": "F1"}))
    p = _provider(json.dumps(expired))
    p.execute(_ctx())
    assert route.called and create.calls[0].request.headers["Authorization"].endswith("refreshed-access-token-abcdefghij")
    assert route.calls[0].request.content.decode().count("grant_type=refresh_token") == 1


# ---------------------------------------------------------------------------------------------- operations


@respx.mock
def test_create_folder_carries_markers_and_parent():
    route = respx.post(f"{gg.DRIVE_API}/files").mock(return_value=httpx.Response(200, json={
        "id": "F9", "name": "02. Requirements", "webViewLink": "https://drive.google.com/drive/folders/F9"}))
    p = _provider()
    res = p.execute(_ctx(logical_id="02-requirements", title="02. Requirements", parent_external_id="F1"))
    body = json.loads(route.calls[0].request.content)
    assert body["mimeType"] == gg.FOLDER_MIME and body["parents"] == ["F1"]
    assert body["appProperties"]["aeos_logical_id"] == "02-requirements"
    assert body["appProperties"]["aeos_project_id"] == "proj"
    assert res.external_id == "F9" and res.kind == "folder"
    root = p.execute(_ctx())  # project folder goes to My Drive root: no parents
    assert "parents" not in json.loads(route.calls[1].request.content) and root.external_id == "F9"


@respx.mock
def test_create_doc_uploads_docx_with_conversion_and_records_revision():
    up = respx.post(f"{gg.UPLOAD_API}/files").mock(return_value=httpx.Response(200, json={
        "id": "D1", "name": "PRD", "webViewLink": "https://docs.google.com/document/d/D1/edit", "mimeType": gg.GDOC_MIME}))
    rev = respx.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=httpx.Response(200, json={
        "documentId": "D1", "revisionId": "rev-1"}))
    res = _provider().execute(_ctx(operation="create_doc", logical_id="02-requirements/prd-x", title="PRD — X",
                                   parent_external_id="F2", attachments={"document.docx": b"PK\x03\x04docx"}))
    req = up.calls[0].request
    assert req.url.params["uploadType"] == "multipart"
    assert req.headers["Content-Type"].startswith("multipart/related; boundary=")
    raw = req.content
    assert gg.DOCX_MIME.encode() in raw and b'"mimeType": "application/vnd.google-apps.document"' in raw
    assert b'"aeos_logical_id": "02-requirements/prd-x"' in raw and b"PK\x03\x04docx" in raw
    assert res.external_revision == "rev-1" and rev.called


@respx.mock
def test_human_edit_detected_by_revision_is_conflict_without_overwrite():
    respx.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=httpx.Response(200, json={
        "documentId": "D1", "revisionId": "rev-HUMAN",
        "body": {"content": [{"endIndex": 1}, {"paragraph": {"elements": [{"textRun": {"content": "edit manusia\n"}}]},
                              "endIndex": 14}]}}))
    batch = respx.post(f"{gg.DOCS_API}/documents/D1:batchUpdate").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ProviderConflict) as exc:
        _provider().execute(_ctx(provider="google-docs", operation="update_doc", logical_id="00-product-home",
                                 existing_external_id="D1", existing_revision="rev-1",
                                 payload={"markdown": "# Baru\n\nisi"}))
    assert not batch.called  # nothing was overwritten
    assert exc.value.observed_revision == "rev-HUMAN" and "edit manusia" in exc.value.current_text


@respx.mock
def test_required_revision_id_race_is_conflict():
    respx.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=httpx.Response(200, json={
        "documentId": "D1", "revisionId": "rev-1", "body": {"content": [{"endIndex": 1}, {"endIndex": 20}]}}))
    batch = respx.post(f"{gg.DOCS_API}/documents/D1:batchUpdate").mock(return_value=httpx.Response(400, json={
        "error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "required revision does not match"}}))
    with pytest.raises(ProviderConflict):
        _provider().execute(_ctx(provider="google-docs", operation="update_doc", logical_id="00-product-home",
                                 existing_external_id="D1", existing_revision="rev-1", payload={"markdown": "# A"}))
    sent = json.loads(batch.calls[0].request.content)
    assert sent["writeControl"] == {"requiredRevisionId": "rev-1"}


@respx.mock
def test_update_uses_required_revision_and_returns_new_revision():
    respx.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=httpx.Response(200, json={
        "documentId": "D1", "revisionId": "rev-1", "body": {"content": [{"endIndex": 1}, {"endIndex": 30}]}}))
    batch = respx.post(f"{gg.DOCS_API}/documents/D1:batchUpdate").mock(return_value=httpx.Response(200, json={
        "documentId": "D1", "replies": [{}], "writeControl": {"requiredRevisionId": "rev-2"}}))
    res = _provider().execute(_ctx(provider="google-docs", operation="update_doc", logical_id="x",
                                   existing_external_id="D1", existing_revision="rev-1",
                                   payload={"markdown": "# Judul\n\n- satu\n\nteks ü"}))
    sent = json.loads(batch.calls[0].request.content)
    assert sent["writeControl"]["requiredRevisionId"] == "rev-1"
    kinds = [next(iter(r)) for r in sent["requests"]]
    assert kinds[0] == "deleteContentRange" and "insertText" in kinds and "createParagraphBullets" in kinds
    assert any(r.get("updateParagraphStyle", {}).get("paragraphStyle", {}).get("namedStyleType") == "HEADING_1"
               for r in sent["requests"])
    assert res.external_revision == "rev-2"


@respx.mock
def test_reconcile_finds_by_app_properties_marker_not_title():
    route = respx.get(f"{gg.DRIVE_API}/files").mock(return_value=httpx.Response(200, json={"files": [
        {"id": "F7", "name": "nama apa saja", "mimeType": gg.FOLDER_MIME, "createdTime": "2026-10-18T00:00:00Z"}]}))
    found = _provider().reconcile(_ctx(logical_id="02-requirements", title="02. Requirements"))
    q = route.calls[0].request.url.params["q"]
    assert "appProperties has { key='aeos_logical_id' and value='02-requirements' }" in q
    assert "aeos_project_id" in q and "name" not in q and "trashed = false" in q
    assert found is not None and found.external_id == "F7"
    route.mock(return_value=httpx.Response(200, json={"files": []}))
    assert _provider().reconcile(_ctx(logical_id="02-requirements")) is None


def test_markdown_to_docs_requests_uses_utf16_indices():
    text, reqs = gg.markdown_to_docs_requests("# 😀 Judul\n\n- a\n")
    heading = next(r for r in reqs if "updateParagraphStyle" in r and
                   r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_1")
    assert heading["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 1 + len("😀 Judul\n") + 1}
    assert text.startswith("😀 Judul\n")


# ---------------------------------------------------------------------------------------------- end-to-end


class DriveSim:
    """Minimal Drive/Docs simulator for respx: keeps created files and their appProperties."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.n = 0
        self.timeout_once: set[str] = set()
        self.human_edits: dict[str, tuple[str, str]] = {}  # doc id -> (revision, text)

    def _new(self, prefix: str, meta: dict) -> str:
        self.n += 1
        fid = f"{prefix}{self.n}"
        self.files[fid] = meta
        return fid

    def create_folder(self, request: httpx.Request) -> httpx.Response:
        meta = json.loads(request.content)
        fid = self._new("F", meta)
        return httpx.Response(200, json={"id": fid, "name": meta["name"],
                                         "webViewLink": f"https://drive.google.com/drive/folders/{fid}"})

    def upload(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        meta = json.loads(raw.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
        fid = self._new("D", meta)
        lid = meta["appProperties"]["aeos_logical_id"]
        if lid in self.timeout_once:
            self.timeout_once.discard(lid)
            raise httpx.ReadTimeout("response lost after the file was created")
        return httpx.Response(200, json={"id": fid, "name": meta["name"], "mimeType": gg.GDOC_MIME,
                                         "webViewLink": f"https://docs.google.com/document/d/{fid}/edit"})

    def list_files(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        lid = re.search(r"key='aeos_logical_id' and value='([^']+)'", q).group(1)
        pid = re.search(r"key='aeos_project_id' and value='([^']+)'", q).group(1)
        hits = [{"id": fid, "name": m["name"], "mimeType": m.get("mimeType"), "createdTime": f"t{fid}"}
                for fid, m in self.files.items()
                if m["appProperties"]["aeos_logical_id"] == lid and m["appProperties"]["aeos_project_id"] == pid]
        return httpx.Response(200, json={"files": hits})

    def get_doc(self, request: httpx.Request) -> httpx.Response:
        doc_id = request.url.path.rsplit("/", 1)[1]
        if doc_id in self.human_edits:
            rev, text = self.human_edits[doc_id]
            return httpx.Response(200, json={"documentId": doc_id, "revisionId": rev, "body": {"content": [
                {"endIndex": 1}, {"paragraph": {"elements": [{"textRun": {"content": text}}]}, "endIndex": 40}]}})
        return httpx.Response(200, json={"documentId": doc_id, "revisionId": f"rev-{doc_id}",
                                         "body": {"content": [{"endIndex": 1}, {"endIndex": 10}]}})


@pytest.fixture
def google_env(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    seed_personal_secrets(secrets_memory)
    from aeos.publisher import providers

    monkeypatch.setitem(providers._overrides, "google", lambda cfg: _provider(root=cfg.get("root_folder_id")))
    project = make_project(db)
    plan, version = make_plan(db, project)
    sim = DriveSim()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{gg.DRIVE_API}/files").mock(side_effect=sim.create_folder)
        mock.post(f"{gg.UPLOAD_API}/files").mock(side_effect=sim.upload)
        mock.get(f"{gg.DRIVE_API}/files").mock(side_effect=sim.list_files)
        mock.get(url__regex=rf"{re.escape(gg.DOCS_API)}/documents/[^/:]+$").mock(side_effect=sim.get_doc)
        perms = mock.route(url__regex=r".*/permissions.*").mock(return_value=httpx.Response(500))
        yield {"db": db, "settings": settings, "plan": plan, "project": project, "sim": sim, "perms": perms,
               "mock": mock}


def _publish_docs(env):
    db = env["db"]
    pub = jobs.create_preview(db, env["settings"], plan_id=env["plan"].id, actor_user_id="owner",
                              options={"targets": ["docs"]}, render_visuals=False)
    db.commit()
    out = jobs.execute_publication(db, pub.id, actor_user_id="owner", confirm=confirm_of(pub))
    db.commit()
    assert out.allowed, out.reasons
    return pub


def test_end_to_end_publish_to_drive_without_public_permissions(google_env):
    db, sim = google_env["db"], google_env["sim"]
    pub = _publish_docs(google_env)
    jobs.process_publication(db, pub.id)
    db.refresh(pub)
    assert pub.status == "PUBLISHED", pub.status_reason
    assert not google_env["perms"].called  # S21: never anyone-with-link / public permissions
    root = next(m for m in sim.files.values() if m["appProperties"]["aeos_logical_id"] == "root")
    assert root["name"] == google_env["project"].name and "parents" not in root
    prd = next(m for m in sim.files.values() if m["appProperties"]["aeos_logical_id"] == "02-requirements/prd-tasks")
    req_folder_id = next(fid for fid, m in sim.files.items() if m["appProperties"]["aeos_logical_id"] == "02-requirements")
    assert prd["parents"] == [req_folder_id]
    ref = db.scalar(select(ExternalReference).where(ExternalReference.logical_id == "00-product-home"))
    assert ref.provider == "google-drive" and ref.external_revision == f"rev-{ref.external_id}"
    leaked = " ".join(json.dumps(a.details) + a.reason for a in db.scalars(select(AuditEvent)))
    assert "ya29" not in leaked and "csecret-value" not in leaked


def test_timeout_after_create_is_reconciled_without_duplicate_file(google_env):
    db, sim = google_env["db"], google_env["sim"]
    sim.timeout_once.add("00-product-home")
    pub = _publish_docs(google_env)
    jobs.process_publication(db, pub.id)
    op = next(o for o in jobs._ops_for(db, pub.id) if o.logical_id == "00-product-home")
    assert op.status == "UNKNOWN"
    jobs.process_publication(db, pub.id, now=utcnow() + timedelta(hours=1))
    db.refresh(op)
    assert op.status == "SUCCEEDED" and op.response.get("reconciled")
    copies = [m for m in sim.files.values() if m["appProperties"]["aeos_logical_id"] == "00-product-home"]
    assert len(copies) == 1  # no duplicate Google Doc


def test_human_edit_in_google_docs_conflicts_and_becomes_revision_input(google_env):
    db, mock = google_env["db"], google_env["mock"]
    pub = _publish_docs(google_env)
    jobs.process_publication(db, pub.id)
    # A human edits the Product Home in Google Docs; then a newer approved version changes it too.
    import copy

    from aeos.models.planning import Plan
    from tests.unit.test_publisher_support import SAMPLE_CONTENT, add_version, approve

    plan = db.get(Plan, google_env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["stack"].append("PostgreSQL")
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    ref = db.scalar(select(ExternalReference).where(ExternalReference.logical_id == "00-product-home"))
    google_env["sim"].human_edits[ref.external_id] = ("rev-human-edit", "Catatan PO\n")
    batch = mock.post(url__regex=r".*:batchUpdate$").mock(return_value=httpx.Response(200, json={
        "writeControl": {"requiredRevisionId": "rev-after-aeos"}}))
    pub2 = _publish_docs(google_env)
    jobs.process_publication(db, pub2.id)
    db.refresh(pub2)
    op = next(o for o in jobs._ops_for(db, pub2.id) if o.logical_id == "00-product-home")
    assert op.status == "CONFLICT" and op.operation == "update_doc"
    assert not batch.called
    assert pub2.status in ("PARTIAL", "CONFLICT")
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "00-product-home"))
    rev = db.scalar(select(DocumentRevision).where(DocumentRevision.document_id == rec.id,
                                                   DocumentRevision.kind == "HUMAN_EDIT_INPUT"))
    assert rev.status == "OPEN" and rev.observed_revision == "rev-human-edit" and "Catatan PO" in rev.body_markdown
    db.refresh(ref)
    assert ref.sync_status == "CONFLICT"
    # a further preview of the same version keeps it CONFLICT (open human edit is never overwritten)
    pub3 = jobs.create_preview(db, google_env["settings"], plan_id=plan.id, actor_user_id="owner",
                               options={"targets": ["docs"]}, render_visuals=False)
    op3 = next(o for o in jobs._ops_for(db, pub3.id) if o.logical_id == "00-product-home")
    assert op3.status == "CONFLICT"
