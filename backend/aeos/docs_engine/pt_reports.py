"""Registration of an external penetration-test report as project evidence (master §27 04. "Vulnerability
assessments and penetration tests ... hanya dari hasil nyata").

A penetration test is performed by a person or vendor outside AEOS. Its real result is their report. The owner
uploads it through the normal intake pipeline (project attachment: type check, scan, extraction, redaction) and
registers it here; AEOS then records a trusted ``PT_REPORT`` EvidenceArtifact from which Product 1 renders its
04 Penetration Test document. Honest by construction:

* the evidence states ``provenance: EXTERNAL_HUMAN_PROVIDED`` - AEOS did not perform or verify the test;
* it references the uploaded file by attachment id and SHA-256 (the stored text is the intake's redacted
  extraction, never the raw file again);
* tester, date and scope are required (a report without them is not a result); the rules of engagement stay a
  HUMAN section of the document;
* the attachment must belong to this project and domain and must not have been rejected by intake scanning.

An external VULNERABILITY ASSESSMENT report (a product's own scanner output or a vendor report) is registered the
same way as ``VA_REPORT`` evidence (``register_va_report``): this is the VA path for a product project, whose
dependencies the platform scanner (``aeos inventory va-scan``) does not cover. It additionally requires the
method/tools and refuses a report without readable content (an empty scan is not a result).

Services take a Session and never commit.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, NotFound, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.models.portfolio import Project

COLLECTOR = "pt-report-registration (human-provided)"
PROVENANCE = "EXTERNAL_HUMAN_PROVIDED"
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
EXCERPT_CHARS = 1500  # keeps the evidence content inline (INLINE_MAX_CHARS)


def _register_external_report(session: Session, project: Project, *, kind: str, code: str, label: str,
                              attachment_id: str, tester: str, performed_on: str, scope: str, method: str,
                              remediation: str, environment: str, commit: str | None, actor: str, actor_type: str,
                              require_method: bool = False, require_text: bool = False,
                              findings: str = "") -> dict[str, Any]:
    """Shared intake/provenance/fail-closed rules of an externally performed security test report."""
    from aeos.intake import facade as intake
    from aeos.verification import facade as verification

    att = intake.get_attachment(session, attachment_id)
    if att is None or att.project_id != project.id or att.domain != project.domain:
        raise NotFound("attachment not found in this project")  # no cross-project/domain metadata leak
    if att.scan_status == "REJECTED":
        raise Blocked("the report was rejected by intake scanning; it cannot be registered", code="ATTACHMENT_REJECTED")
    try:
        day = date.fromisoformat((performed_on or "").strip())
    except ValueError as exc:
        raise ValidationFailed("performed_on must be an ISO date (YYYY-MM-DD)", code=code,
                               details={"field": "performed_on"}) from exc
    if day > date.today():
        raise ValidationFailed("performed_on cannot be in the future", code=code, details={"field": "performed_on"})
    sha = (commit or "").strip().lower() or None
    if sha is not None and not _SHA.match(sha):
        raise ValidationFailed("commit must be a git SHA (7-64 hex characters)", code="SHA_INVALID")

    def text(value: str | None, *, field: str, minimum: int = 0, maximum: int = 2000) -> str:
        out = redact_text((value or "").strip())[:maximum]
        if len(out) < minimum:
            raise ValidationFailed(f"{field} is required (at least {minimum} characters)", code=code,
                                   details={"field": field})
        return out

    who = text(tester, field="tester", minimum=2, maximum=200)
    what = text(scope, field="scope", minimum=10)
    how = text(method, field="method", minimum=2 if require_method else 0)
    excerpt = intake.attachment_text(session, att.id, max_chars=EXCERPT_CHARS)  # redacted intake extraction
    if require_text and not (excerpt or "").strip():
        # a scan/assessment without readable results is not a result: nothing is registered (fail closed)
        raise Blocked("the report has no readable content; an empty report is not an assessment result",
                      code=f"{kind}_EMPTY")
    content = {
        "provenance": PROVENANCE,
        "note": "Laporan eksternal yang didaftarkan operator; AEOS tidak melakukan atau memverifikasi pengujian ini.",
        "tester": who, "performed_on": day.isoformat(), "scope": what, "method": how, "tools": how,
        "remediation": text(remediation, field="remediation"),
        "report": {"attachment_id": att.id, "filename": att.filename, "sha256": att.sha256,
                   "size_bytes": att.size_bytes, "detected_type": att.detected_type},
        "excerpt": excerpt,
        **({"findings_summary": text(findings, field="findings")} if findings else {}),
    }
    ev = verification.collect_evidence(
        session, kind=kind, origin="collector", collector_identity=COLLECTOR,
        claim=f"External {label} by {who} on {day.isoformat()}: {what[:120]}",
        content=content, evidence_level="E1", subject_type="attachment", subject_id=att.id,
        project_id=project.id, domain=project.domain,
        environment={"name": text(environment, field="environment", maximum=64) or None, "commit": sha,
                     "performed_on": day.isoformat(), "provenance": PROVENANCE})
    audit.record(session, actor_type=actor_type, actor_id=actor, action=f"docs.{kind.lower()}.register",
                 result="SUCCESS", target_type="project", target_id=project.id, project_id=project.id,
                 domain=project.domain, details={"evidence_id": ev.id, "attachment_id": att.id, "sha256": att.sha256})
    return {"evidence_id": ev.id, "attachment_id": att.id, "sha256": att.sha256, "provenance": PROVENANCE,
            "kind": kind}


def register_pt_report(session: Session, project: Project, *, attachment_id: str, tester: str, performed_on: str,
                       scope: str, method: str = "", remediation: str = "", environment: str = "",
                       commit: str | None = None, actor: str, actor_type: str = "cli") -> dict[str, Any]:
    return _register_external_report(
        session, project, kind="PT_REPORT", code="PT_REPORT_INVALID", label="penetration test",
        attachment_id=attachment_id, tester=tester, performed_on=performed_on, scope=scope, method=method,
        remediation=remediation, environment=environment, commit=commit, actor=actor, actor_type=actor_type)


def register_va_report(session: Session, project: Project, *, attachment_id: str, assessor: str, performed_on: str,
                       scope: str, tools: str, findings: str = "", remediation: str = "", environment: str = "",
                       commit: str | None = None, actor: str, actor_type: str = "cli") -> dict[str, Any]:
    """§27 04. Vulnerability assessment of ANY project (a product's own scanner/vendor report) as ``VA_REPORT``
    evidence, under the same rules as a PT report. The method/tools are required (an assessment names how it was
    done) and a report without readable content is refused (no empty or no-result scan becomes a document)."""
    return _register_external_report(
        session, project, kind="VA_REPORT", code="VA_REPORT_INVALID", label="vulnerability assessment",
        attachment_id=attachment_id, tester=assessor, performed_on=performed_on, scope=scope, method=tools,
        remediation=remediation, environment=environment, commit=commit, actor=actor, actor_type=actor_type,
        require_method=True, require_text=True, findings=findings)
