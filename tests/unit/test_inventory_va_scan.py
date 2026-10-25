"""§27 04. Vulnerability assessments only from real results: a trusted collector runs pip-audit on the platform's
lockfile and stores VA_REPORT evidence (commit recorded), which Product 1 then renders; missing scanner or no result
-> refused, nothing stored (never an empty "no findings" report)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, ValidationFailed
from aeos.docs_engine import service as docs
from aeos.inventory import va_scan
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import Project

REPORT = {"dependencies": [
    {"name": "jinja2", "version": "3.1.3", "vulns": [{"id": "GHSA-h5c8-rqwp-cp95", "aliases": ["CVE-2024-22195"],
                                                     "fix_versions": ["3.1.4"]}]},
    {"name": "fastapi", "version": "0.115.0", "vulns": []}]}


def _runner(*, installed: bool = True, rc: int = 1, out: str | None = None):
    calls = []

    def run(argv):
        calls.append(argv)
        if "--version" in argv:
            return (0, "pip-audit 2.7.3\n", "") if installed else (1, "", "No module named pip_audit")
        return rc, json.dumps(REPORT) if out is None else out, ""

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _platform(db) -> Project:
    p = Project(project_key="dly-aeos", name="DLY AEOS", domain="PERSONAL", data_classification="INTERNAL",
                is_platform=True)
    db.add(p)
    db.commit()
    return p


def test_scan_stores_trusted_va_evidence_with_findings_and_fixes(settings, db):
    p = _platform(db)
    out = va_scan.scan_platform_dependencies(db, p, actor="cli:test", commit="c" * 40, dirty=False, runner=_runner())
    db.commit()
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.kind == "VA_REPORT" and ev.trusted and ev.origin == "collector" and ev.project_id == p.id
    assert ev.environment["commit"] == "c" * 40
    assert out["findings"] == 1 and out["fixes"] == ["jinja2 3.1.3 -> 3.1.4 (GHSA-h5c8-rqwp-cp95)"]
    refs = [e for e in docs.evidence_refs(db, p) if e.kind == "VA_REPORT"]
    assert refs and "requirements.lock" in refs[0].get("scope") and "pip-audit 2.7.3" in refs[0].get("method")
    assert "3.1.4" in refs[0].get("remediation")


@pytest.mark.parametrize(("runner", "code"), [(_runner(installed=False), "VA_SCANNER_MISSING"),
                                              (_runner(rc=2, out="boom"), "VA_SCAN_FAILED"),
                                              (_runner(rc=0, out=""), "VA_SCAN_FAILED")])
def test_no_scanner_or_no_result_is_refused_and_nothing_is_stored(settings, db, runner, code):
    p = _platform(db)
    with pytest.raises(Blocked) as exc:
        va_scan.scan_platform_dependencies(db, p, actor="cli:test", runner=runner)
    assert exc.value.code == code
    assert not list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "VA_REPORT")))


def test_only_the_platform_lockfile_is_scanned(settings, db):
    product = Project(project_key="x", name="X", domain="PERSONAL", data_classification="INTERNAL")
    db.add(product)
    db.commit()
    with pytest.raises(ValidationFailed):
        va_scan.scan_platform_dependencies(db, product, actor="cli:test", runner=_runner())
