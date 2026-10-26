"""§29 Google publication: DOCX -> Google Docs import fidelity is checked (headings/tables/links/images via
documents.get), and an image update never runs as a lossy text-only update - a new DOCX snapshot is planned under
the publication's own human decision instead ("Update incremental gambar memerlukan jalur akses yang disetujui;
jika belum ada, siapkan snapshot baru dan keputusan publikasi")."""

from __future__ import annotations

import copy
import struct
import zlib
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.hashing import content_hash
from aeos.docs_engine.docx import markdown_to_docx
from aeos.models.planning import Plan
from aeos.models.publishing import DocumentRecord
from aeos.publisher import fidelity, jobs
from aeos.publisher.providers import google as gg
from aeos.publisher.providers.base import OpContext
from tests.unit.test_publisher_support import (
    SAMPLE_CONTENT,
    add_version,
    approve,
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)

MD = ("# Product Home\n\nIntro with a [spec link](https://example.org/spec).\n\n## Scope\n\n"
      "| Key | Value |\n|---|---|\n| a | [doc](https://example.org/doc) |\n| b | 2 |\n\n"
      "![Context](visual:ctx)\n\n### Notes\n\nplain\n")


def _png(path: Path) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"\x00\xff\xff\xff"  # one white pixel
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def _gdoc(*, headings=(("1", "Product Home"), ("2", "Scope"), ("3", "Notes")), table=(3, 2),
          links=("https://example.org/spec", "https://example.org/doc"), images=1) -> dict:
    content = [{"paragraph": {"paragraphStyle": {"namedStyleType": f"HEADING_{lvl}"},
                              "elements": [{"textRun": {"content": f"{text}\n"}}]}} for lvl, text in headings]
    content.append({"paragraph": {"elements": [{"textRun": {"content": "Intro with a "}},
                                               {"textRun": {"content": "spec link",
                                                            "textStyle": {"link": {"url": links[0]}}}}]}})
    if table:
        cells = [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "doc", "textStyle": {
            "link": {"url": u}}}}]}}]} for u in links[1:]]
        content.append({"table": {"rows": table[0], "columns": table[1], "tableRows": [{"tableCells": cells}]}})
    return {"documentId": "D1", "revisionId": "rev-1", "body": {"content": content},
            "inlineObjects": {f"kix.{i}": {} for i in range(images)}}


@pytest.fixture
def docx_bytes(tmp_path):
    png = _png(tmp_path / "ctx.png")
    return markdown_to_docx(MD, title="Product Home", logical_id="00-product-home",
                            image_resolver=lambda key: png if key == "ctx" else None)


def test_docx_structure_is_what_was_sent(docx_bytes):
    s = fidelity.docx_structure(docx_bytes)
    assert s["headings"] == [[1, "Product Home"], [2, "Scope"], [3, "Notes"]]
    assert s["tables"] == [[3, 2]] and s["images"] == 1
    assert s["links"] == ["https://example.org/doc", "https://example.org/spec"]


@pytest.mark.parametrize("observed,needle", [
    (dict(headings=(("1", "Product Home"), ("1", "Scope"), ("3", "Notes"))), "headings"),  # level lost
    (dict(table=None), "tables"),
    (dict(links=("https://example.org/spec",)), "links"),
    (dict(images=0), "images"),
])
def test_any_lost_heading_table_link_or_image_is_a_mismatch(docx_bytes, observed, needle):
    expected = fidelity.docx_structure(docx_bytes)
    assert fidelity.compare(expected, fidelity.gdoc_structure(_gdoc()))["status"] == "CONSISTENT"
    out = fidelity.compare(expected, fidelity.gdoc_structure(_gdoc(**observed)))
    assert out["status"] == "MISMATCH" and any(m.startswith(needle) for m in out["mismatches"])
    assert fidelity.compare(expected, None)["status"] == "UNKNOWN"  # unreadable is never a pass


def _ctx(docx: bytes) -> OpContext:
    return OpContext(operation_id="op1", publication_id="pub1", project_id="p1", project_key="demo",
                     project_name="Demo", domain="PERSONAL", provider="google-drive", operation="create_doc",
                     logical_id="00-product-home", idempotency_key="sha256:" + "a" * 64, title="Product Home",
                     target={}, payload={}, parent_external_id="F1", attachments={"document.docx": docx})


class _Auth:
    def header(self):
        return {"Authorization": "Bearer test"}

    def refresh(self):
        raise AssertionError("no refresh expected")


@pytest.mark.parametrize("doc_response,status,revision", [
    (httpx.Response(200, json=_gdoc()), "CONSISTENT", "rev-1"),
    (httpx.Response(200, json=_gdoc(table=None, images=0)), "MISMATCH", "rev-1"),
    (httpx.Response(500, json={}), "UNKNOWN", None),
])
def test_google_import_reads_the_document_back_and_records_fidelity(docx_bytes, doc_response, status, revision):
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(f"{gg.UPLOAD_API}/files").mock(return_value=httpx.Response(200, json={
            "id": "D1", "name": "Product Home", "webViewLink": "https://docs.google.com/document/d/D1/edit",
            "mimeType": gg.GDOC_MIME}))
        mock.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=doc_response)
        prov = gg.GoogleDriveDocsProvider(client=httpx.Client(), auth=_Auth())
        res = prov.execute(_ctx(docx_bytes))
    assert res.response["fidelity"]["status"] == status and res.external_revision == revision
    assert res.response["revision_unknown"] is (revision is None)


def test_unparseable_snapshot_never_raises_over_the_completed_write():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(f"{gg.UPLOAD_API}/files").mock(return_value=httpx.Response(200, json={"id": "D1"}))
        mock.get(f"{gg.DOCS_API}/documents/D1").mock(return_value=httpx.Response(200, json=_gdoc()))
        res = gg.GoogleDriveDocsProvider(client=httpx.Client(), auth=_Auth()).execute(_ctx(b"PK\x03\x04no-docx"))
    assert res.external_id == "D1" and res.response["fidelity"]["status"] == "UNKNOWN"


# ------------------------------------------------------------------------------- image update -> DOCX snapshot


@pytest.fixture
def env(settings, db, secrets_memory, monkeypatch):
    policy = install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, version = make_plan(db, project)
    return {"settings": settings, "db": db, "policy": policy, "fakes": fakes, "project": project, "plan": plan,
            "version": version, "monkeypatch": monkeypatch}


def _with_rendered_image(env, logical_id="00-product-home"):
    """The docs engine output with one rendered diagram embedded in *logical_id* (mmdc is not run in tests)."""
    from aeos.docs_engine import service as docs_service
    from aeos.docs_engine import visuals as vis

    real = docs_service.render_version
    png = _png(env["settings"].layout.root / "ctx.png")

    def render_version(session, settings, version, **kw):
        result, delta, state = real(session, settings, version, **kw)
        for d in result.documents:
            if d.logical_doc_id == logical_id:
                body = docs_service.document_body(session, version.project_id, d) + "\n\n![Context](visual:ctx)\n"
                d.body_markdown = body
                d.content_hash = content_hash({"markdown": body})
        result.visuals.append(vis.VisualArtifact(key="ctx", title="Context", kind="context", source="flowchart TD",
                                                 source_hash="h", status=vis.STATUS_RENDERED, png_path=str(png)))
        return result, delta, state

    # the publisher renders through aeos.docs_engine.facade, which calls this service implementation
    env["monkeypatch"].setattr(docs_service, "render_version", render_version)


def _preview(env):
    pub = jobs.create_preview(env["db"], env["settings"], plan_id=env["plan"].id, actor_user_id="owner-user",
                              render_visuals=False)
    env["db"].commit()
    return pub


def _run(env, pub):
    out = jobs.execute_publication(env["db"], pub.id, actor_user_id="owner-user", confirm=confirm_of(pub))
    env["db"].commit()
    assert out.allowed, out.reasons
    jobs.process_publication(env["db"], pub.id)
    env["db"].commit()


def _approve_v2(env):
    db = env["db"]
    plan = db.get(Plan, env["plan"].id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["summary"] = "Versi 2 dengan diagram"
    approve(db, plan, add_version(db, plan, content))
    db.commit()


def test_image_update_becomes_a_new_docx_snapshot_never_a_lossy_update(env):
    db = env["db"]
    _run(env, _preview(env))  # v1 published (create_doc for every document)
    first_ext = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "00-product-home")).links[
        "external_id"]
    _approve_v2(env)
    _with_rendered_image(env)
    pub = _preview(env)
    ops = {(o.operation, o.logical_id): o for o in jobs._ops_for(db, pub.id)}
    snap_id = next(lid for (op, lid) in ops if lid.startswith("00-product-home@snapshot-"))
    snap = ops[("create_doc", snap_id)]
    assert ("update_doc", "00-product-home") not in ops  # the lossy text-only update is not planned
    assert snap.status == "PENDING" and snap.payload["snapshot_of"] == "00-product-home"
    assert snap.payload["rendered_visuals"] == ["ctx"] and snap.payload["previous_external_id"] == first_ext
    assert any("snapshot DOCX baru dengan gambar tertanam" in n for n in pub.preview["notes"])
    assert jobs.operation_view(snap)["payload_summary"]["snapshot_of"] == "00-product-home"
    calls_before = list(env["fakes"]["google"].calls)
    _run(env, pub)  # the human's publication decision of this preview covers the snapshot operation
    new_calls = env["fakes"]["google"].calls[len(calls_before):]
    assert ("create_doc", snap_id) in new_calls and ("update_doc", "00-product-home") not in new_calls
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "00-product-home"))
    assert rec.links["snapshot"]["logical_id"] == snap_id and rec.links["visual_update_pending"] is False
    assert rec.links["snapshot"]["previous_external_id"] == first_ext  # the old document was not overwritten
    again = _preview(env)  # same content: the snapshot is not created twice
    assert not [o for o in jobs._ops_for(db, again.id) if o.logical_id == snap_id and o.status == "PENDING"]
    assert any(d["logical_id"] == snap_id and d["status"] == "SUCCEEDED" for d in again.preview["already_done"])


def test_superseded_document_banners_its_snapshot_too(env):
    """A document last published as a DOCX snapshot that a newer version drops gets the SUPERSEDED banner on the
    snapshot document as well (nothing deleted, update under revision control)."""
    from aeos.docs_engine import renderer as rnd
    from aeos.models.publishing import ExternalReference
    from aeos.publisher.targets import Target

    db, project = env["db"], env["project"]
    rec = DocumentRecord(project_id=project.id, domain="PERSONAL", logical_doc_id="05-old-prd", node="05",
                         title="Old PRD", kind="DOC", status=rnd.ST_SUPERSEDED, doc_type="PRD",
                         links={"snapshot": {"logical_id": "05-old-prd@snapshot-abc123abc123"}})
    db.add(rec)
    for lid, ext in (("05-old-prd", "D-old"), ("05-old-prd@snapshot-abc123abc123", "D-snap")):
        db.add(ExternalReference(project_id=project.id, domain="PERSONAL", provider="google-drive", logical_id=lid,
                                 external_id=ext, external_revision="r1", content_hash="sha256:" + "0" * 64))
    db.commit()
    target = Target(purpose="docs", family="google", label="Google", status="READY", provider="google-drive",
                    root_id="F1")
    result = rnd.RenderResult(project_id=project.id, plan_version_id=env["version"].id, approved=True,
                              product_title="x", documents=[])
    ops = jobs._superseded_ops(db, project, env["version"], result, target, {"05-old-prd": rec})
    assert [(o.operation, o.logical_id, o.target["external_id"]) for o in ops] == [
        ("update_doc", "05-old-prd", "D-old"), ("update_doc", "05-old-prd@snapshot-abc123abc123", "D-snap")]
    assert all(o.payload["superseded"] and o.title.startswith(jobs.SUPERSEDED_TITLE_PREFIX) for o in ops)


def test_snapshot_never_leaves_acknowledged_human_content_behind(env, monkeypatch):
    db = env["db"]
    _run(env, _preview(env))
    _approve_v2(env)
    _with_rendered_image(env)
    monkeypatch.setattr(jobs, "_human_guard", lambda *a, **k: {"human_content_possible": True, "sections": []})
    pub = _preview(env)
    snap = next(o for o in jobs._ops_for(db, pub.id) if o.logical_id.startswith("00-product-home@snapshot-"))
    assert snap.status == "CONFLICT" and "HUMAN_GOVERNED" in snap.error
