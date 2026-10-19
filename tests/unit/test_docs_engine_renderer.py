"""Renderer honesty, content types, §28 timing and hash-based delta rendering (§27-§29, §41)."""

from __future__ import annotations

import copy
import re

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict
from aeos.docs_engine import product1 as p1
from aeos.docs_engine import renderer as r
from aeos.docs_engine import service
from aeos.models.planning import Plan
from aeos.models.publishing import DocumentRecord, DocumentRevision
from tests.unit.test_publisher_support import (
    SAMPLE_CONTENT,
    add_version,
    approve,
    install_fakes,
    make_plan,
    make_project,
)

PROJECT = r.ProjectIdentity(id="p-1", key="contoh", name="Contoh App", domain="PERSONAL", owner_name="Owner",
                            repositories=(r.RepoInfo("GITHUB", "owner/contoh", "https://github.com/owner/contoh"),))
VERSION = r.VersionIdentity(id="v-1", plan_id="plan-1", version=1, manifest_hash="sha256:" + "a" * 64, approved=True)


def _render(content=None, *, project=PROJECT, version=VERSION, tasks=None, previous=None):
    return r.render_document_set(project=project, version=version,
                                 content=copy.deepcopy(SAMPLE_CONTENT) if content is None else content,
                                 tasks=tasks or [], previous_input_hashes=previous)


def test_project_creation_and_feature_approval_sets_are_rendered():
    res = _render()
    ids = res.by_id()
    for lid in ("root", "00-product-home", "01-product-overview", "03-architecture-overview", "04-security-overview",
                "04-security-assessments/initial-risk-assessment", "04-data-inventory", "05-development",
                "02-requirements/prd-tasks", "02-requirements/prd-auth", "06-testing/test-plan-tasks",
                "06-testing/test-plan-auth", "03-tdds/tdd-tasks", "03-rfcs/rfc-auth",
                "04-threat-models/threat-model-initial", "04-privacy-assessments/dpia-auth"):
        assert lid in ids, lid
    assert "04-privacy-assessments/dpia-tasks" not in ids  # personal_data False → no DPIA
    assert len([d for d in res.documents if d.doc_type == "TEMPLATE"]) == 12
    home = ids["00-product-home"]
    assert res.product_title in home.body_markdown and home.kind == "DOC"
    assert home.status == "APPROVED"


def test_future_nodes_are_not_started_with_reason_and_owner():
    ids = _render().by_id()
    for lid in ("08-operational-overview", "08-monitoring", "08-logs", "08-alerts", "08-slo", "08-backup-restore",
                "08-maintenance"):
        d = ids[lid]
        assert d.status == "NOT_STARTED" and "production validation" in d.status_reason.lower()
        assert "Owner:" in d.status_reason and d.owner
        assert d.pending_sections == []  # not "documentation pending" — not due yet
        assert "NOT STARTED" in d.body_markdown
    retire = ids["12-retirement"]
    assert retire.status == "NOT_APPLICABLE" and "retirement" in retire.status_reason.lower()
    for lid in ("07-release-deployment", "04-vulnerability-assessments", "04-penetration-tests", "08-runbooks",
                "09-incidents", "09-problems", "10-changes", "11-improvements"):
        assert ids[lid].kind == "FOLDER" and ids[lid].status == "NOT_STARTED" and ids[lid].status_reason


def test_renderer_never_invents_results_or_approvals():
    res = _render()
    text = "\n".join(d.body_markdown or "" for d in res.documents if d.doc_type != "TEMPLATE")
    assert not re.search(r"PASS-(STATIC|CONTROLLED|STAGING|PRODUCTION)", text)
    assert not re.search(r"(?i)\b(approved by|disetujui oleh|signed off by|lulus uji)\b", text)
    tp = res.by_id()["06-testing/test-plan-tasks"]
    secs = {s.key: s for s in tp.sections}
    assert secs["execution_results"].status == r.SEC_LIVE and secs["uat"].status == r.SEC_LIVE
    assert secs["sign_off"].status == r.SEC_HUMAN and secs["final_assessment"].status == r.SEC_HUMAN
    # §28: DYNAMIC sections reference the live source; no copied "NOT STARTED"/PASS snapshot that goes stale
    assert "NOT STARTED" not in tp.body_markdown and "tidak menyatakan hasil uji" in tp.body_markdown
    assert "| TC-1 | User dapat membuat tugas |" in tp.body_markdown and "| Hasil |" not in tp.body_markdown
    ra = res.by_id()["04-security-assessments/initial-risk-assessment"]
    assert {s.key: s.status for s in ra.sections}["acceptance"] == r.SEC_HUMAN


def test_dynamic_sections_reference_live_sources_and_human_sections_are_left_for_humans():
    res = _render()
    for d in res.documents:
        if d.kind != "DOC" or d.doc_type == "TEMPLATE" or d.status != "APPROVED":
            continue
        spec = p1.DOC_TYPES[d.doc_type]
        for s in d.sections:
            ct = spec.section(s.key).content_type.value
            if ct == "DYNAMIC":
                assert s.status == r.SEC_LIVE, (d.logical_doc_id, s.key)
            if ct == "HUMAN_GOVERNED":
                assert s.status == r.SEC_HUMAN, (d.logical_doc_id, s.key)
    assert "Sumber live: GitHub Issues (owner/contoh)" in res.by_id()["00-product-home"].body_markdown


def test_content_type_guard_refuses_filling_human_or_dynamic_sections():
    b = r._Doc(p1.DOC_TYPES["TEST_PLAN"], "x")
    with pytest.raises(ValueError):
        b.put("sign_off", "Signed")
    with pytest.raises(ValueError):
        b.put("execution_results", "all tests pass")
    with pytest.raises(ValueError):
        b.live("objectives", "x")


def test_missing_plan_content_is_documentation_pending_not_invented():
    res = _render({"features": [{"feature_key": "x", "title": "X"}]})
    prd = res.by_id()["02-requirements/prd-x"]
    assert "Problem" in prd.pending_sections and "Success Metrics" in prd.pending_sections
    assert r.PENDING_TEXT in prd.body_markdown
    dev = res.by_id()["05-development"]
    assert len(dev.pending_sections) == len(p1.DOC_TYPES["DEVELOPMENT_GUIDELINES"].sections)
    empty = _render({})
    assert empty.by_id()["02-requirements"].status == "NOT_STARTED"


def test_section_anchors_are_unique_and_embedded():
    res = _render()
    anchors = [s.anchor for d in res.documents for s in d.sections]
    assert len(anchors) == len(set(anchors))
    home = res.by_id()["00-product-home"]
    assert f"anchor={home.sections[0].anchor}" in home.body_markdown
    assert home.body_markdown.count("\n## ") == len(p1.DOC_TYPES["PRODUCT_HOME"].sections)


def test_office_project_uses_jira_and_confluence_wording():
    office = r.ProjectIdentity(id="p-2", key="kantor", name="Kantor", domain="OFFICE")
    home = _render(project=office).by_id()["00-product-home"].body_markdown
    assert "Jira" in home and "Confluence" in home and "GitHub Issues" not in home


def test_unapproved_version_renders_as_draft_preview():
    v = r.VersionIdentity(id="v-2", plan_id="plan-1", version=2, manifest_hash="sha256:" + "b" * 64, approved=False)
    res = _render(version=v)
    assert res.by_id()["00-product-home"].status == "DRAFT"
    assert "BELUM DISETUJUI" in res.by_id()["00-product-home"].body_markdown


def test_delta_render_skips_unchanged_documents_by_input_hash():
    first = _render()
    previous = {d.logical_doc_id: d.input_hash for d in first.documents}
    again = _render(previous=previous)
    docs = [d for d in again.documents if d.kind == "DOC"]
    assert docs and all(d.reused and d.body_markdown is None for d in docs)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"][0]["acceptance"].append("User dapat mengarsipkan tugas")
    changed = _render(content, previous=previous)
    rerendered = {d.logical_doc_id for d in changed.documents if d.kind == "DOC" and not d.reused}
    assert rerendered == {"02-requirements/prd-tasks", "06-testing/test-plan-tasks", "03-tdds/tdd-tasks"}


def test_sync_records_delta_supersede_and_revisions(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db)
    plan, v1 = make_plan(db, project)
    _res, delta, state = service.render_version(db, settings, v1, persist=True, render_visuals=False)
    db.commit()
    assert state.approved and delta.created and not delta.changed
    rec = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "02-requirements/prd-tasks"))
    assert rec.status == "APPROVED" and rec.source_plan_version_id == v1.id and rec.body_markdown
    assert rec.content_type and rec.sections and rec.owner
    # same version again: nothing changes
    _res, delta2, _ = service.render_version(db, settings, v1, persist=True, render_visuals=False)
    assert not delta2.created and not delta2.changed and delta2.unchanged
    # v2 drops the 'auth' feature and edits 'tasks'
    plan = db.get(Plan, plan.id)
    content = copy.deepcopy(SAMPLE_CONTENT)
    content["features"] = [content["features"][0]]
    content["features"][0]["summary"] = "CRUD tugas + arsip."
    v2 = add_version(db, plan, content)
    approve(db, plan, v2)
    db.commit()
    _res, delta3, _ = service.render_version(db, settings, v2, persist=True, render_visuals=False)
    db.commit()
    assert "02-requirements/prd-tasks" in delta3.changed
    assert "02-requirements/prd-auth" in delta3.superseded
    assert "05-development" in delta3.unchanged
    db.refresh(rec)
    assert rec.source_plan_version_id == v2.id
    revs = list(db.scalars(select(DocumentRevision).where(DocumentRevision.document_id == rec.id)))
    assert sorted(x.status for x in revs) == ["CURRENT", "SUPERSEDED"]
    gone = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "02-requirements/prd-auth"))
    assert gone.status == "SUPERSEDED"
    unchanged = db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == "05-development"))
    assert unchanged.source_plan_version_id == v1.id  # content last derived from v1


def test_sync_refuses_unapproved_render():
    res = _render(version=r.VersionIdentity(id="v", plan_id="p", version=1, manifest_hash="sha256:x", approved=False))
    with pytest.raises(Conflict):
        r.sync_document_records(None, res, domain="PERSONAL")  # type: ignore[arg-type]


def test_local_preview_writes_markdown_tree(settings, db, monkeypatch, tmp_path):
    install_fakes(monkeypatch)
    project = make_project(db)
    _plan, v1 = make_plan(db, project)
    res, _d, _s = service.render_version(db, settings, v1, persist=False, render_visuals=False)
    info = service.write_preview(db, res, tmp_path / "out", docx=True)
    assert (tmp_path / "out" / "00-product-home.md").is_file()
    assert (tmp_path / "out" / "02-requirements" / "prd-tasks.md").is_file()
    assert (tmp_path / "out" / "02-requirements" / "prd-tasks.docx").is_file()
    assert (tmp_path / "out" / "INDEX.md").read_text(encoding="utf-8").startswith("# PRODUCT 1 (Contoh App)")
    assert len(info["files"]) == sum(1 for d in res.documents if d.kind == "DOC")
