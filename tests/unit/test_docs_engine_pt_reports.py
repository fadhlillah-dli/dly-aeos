"""§27 04. Penetration tests "hanya dari hasil nyata": an external PT report, uploaded through intake and registered
by the operator, becomes trusted PT_REPORT evidence that states it is EXTERNAL_HUMAN_PROVIDED; the 04 Penetration
Test document is rendered from it (scope, remediation, provenance) and nothing is invented without one."""

from __future__ import annotations

import pytest

from aeos.core.errors import Blocked, NotFound, ValidationFailed
from aeos.docs_engine import renderer as r
from aeos.docs_engine import service
from aeos.docs_engine.pt_reports import PROVENANCE, register_pt_report
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import Attachment, Project
from tests.conftest import AuthedClient
from tests.unit.test_docs_engine_schema_seam import _render, _section_text
from tests.unit.test_portfolio_support import create_project
from tests.unit.test_publisher_support import schema_sample_content

REPORT = (b"# Penetration Test Report\n\nTester: Example Security\n\nScope: login API and session handling.\n\n"
          b"Finding H-1: session fixation on /login (fixed in 1.2).\n")


def _upload(client: AuthedClient, project_id: str, body: bytes = REPORT, name: str = "pt-report.md") -> str:
    res = client.post(f"/api/projects/{project_id}/attachments", files={"file": (name, body, "text/markdown")})
    assert res.status_code == 200, res.text
    return (res.json().get("attachment") or res.json())["id"]


def test_registered_external_report_renders_the_penetration_test_document(personal_client: AuthedClient, db):
    pid = create_project(personal_client)["id"]
    att_id = _upload(personal_client, pid)
    project = db.get(Project, pid)
    out = register_pt_report(db, project, attachment_id=att_id, tester="Example Security", performed_on="2026-09-30",
                             scope="Login API and session handling of the staging deployment",
                             method="OWASP WSTG manual testing", remediation="Rotate session id on login",
                             environment="staging", actor="local-operator")
    db.commit()
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.kind == "PT_REPORT" and ev.trusted and ev.origin == "collector" and ev.evidence_level == "E1"
    assert out["provenance"] == PROVENANCE and ev.subject_id == att_id
    assert ev.inline["content"]["report"]["sha256"] == db.get(Attachment, att_id).sha256

    refs = service.evidence_refs(db, project)
    (ref,) = [x for x in refs if x.kind == "PT_REPORT"]
    assert ref.get("provenance") == PROVENANCE and ref.get("tester") == "Example Security"
    ids = _render(schema_sample_content(), signals=r.RenderSignals(evidence=refs)).by_id()
    pts = [d for k, d in ids.items() if k.startswith("04-security-privacy/04-penetration-tests/pt-")
           or k.startswith("04-penetration-tests/pt-")]
    assert len(pts) == 1, sorted(ids)
    doc = pts[0]
    assert "session handling" in _section_text(doc, "scope")
    assert "Rotate session id" in _section_text(doc, "remediation")
    assert "tidak melakukan atau memverifikasi" in doc.body_markdown
    assert ev.id in doc.links["evidence"]


def test_registration_fails_closed(personal_client: AuthedClient, db):
    pid = create_project(personal_client)["id"]
    other = create_project(personal_client, name="Lain")["id"]
    att_id = _upload(personal_client, pid)
    project = db.get(Project, pid)
    kw = {"tester": "Example Security", "performed_on": "2026-09-30", "scope": "Login API and sessions",
          "actor": "local-operator"}
    with pytest.raises(NotFound):
        register_pt_report(db, db.get(Project, other), attachment_id=att_id, **kw)  # another project's file
    with pytest.raises(ValidationFailed):
        register_pt_report(db, project, attachment_id=att_id, **{**kw, "performed_on": "2999-01-01"})
    with pytest.raises(ValidationFailed):
        register_pt_report(db, project, attachment_id=att_id, **{**kw, "scope": "x"})
    with pytest.raises(ValidationFailed):
        register_pt_report(db, project, attachment_id=att_id, **{**kw, "tester": ""})
    att = db.get(Attachment, att_id)
    att.scan_status = "REJECTED"
    db.flush()
    with pytest.raises(Blocked):
        register_pt_report(db, project, attachment_id=att_id, **kw)
    assert db.query(EvidenceArtifact).filter_by(kind="PT_REPORT").count() == 0


def test_without_a_report_the_penetration_test_folder_stays_not_started():
    ids = _render(schema_sample_content(), signals=r.RenderSignals()).by_id()
    assert not [k for k in ids if "/pt-" in k]
