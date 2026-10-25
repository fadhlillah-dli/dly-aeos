"""Trusted vulnerability-assessment collector (master §27 04. "Vulnerability assessments ... hanya dari hasil nyata",
§25 dependency/licence evidence).

Runs ``pip-audit`` on the platform project's own lockfile (``requirements.lock``) and stores the real result as a
trusted ``VA_REPORT`` EvidenceArtifact (collector origin, commit recorded) for the platform project, from which
Product 1 renders its 04 Vulnerability Assessment document. Fail closed and honest:

* pip-audit missing -> ``Blocked`` with the install command (never an empty "no findings" report);
* pip-audit needs the advisory database (network); an error/timeout -> ``Blocked``, nothing stored;
* the report states what was scanned (lockfile, commit, whether the checkout was dirty), the tool version, the
  findings and the fix versions it names - it is evidence, not a security verdict for the whole product.
Only the platform project is scanned here: a product repository is not read by AEOS.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.paths import repo_root
from aeos.core.redaction import redact_text
from aeos.models.portfolio import Project

COLLECTOR = "va-collector:pip-audit"
INSTALL = "python -m pip install pip-audit==2.7.3"
TIMEOUT_S = 600

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, str, str]]


def _run(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover - real subprocess
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT_S, check=False, cwd=repo_root(), stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _summary(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    findings, fixes = [], []
    for dep in report.get("dependencies") or []:
        for v in dep.get("vulns") or []:
            findings.append({"package": dep.get("name"), "version": dep.get("version"), "id": v.get("id"),
                             "aliases": v.get("aliases") or [], "fix_versions": v.get("fix_versions") or []})
            if v.get("fix_versions"):
                fixes.append(f"{dep.get('name')} {dep.get('version')} -> {', '.join(v['fix_versions'])} ({v.get('id')})")
    return findings, sorted(set(fixes))


def scan_platform_dependencies(session: Session, project: Project, *, actor: str, actor_type: str = "cli",
                               commit: str | None = None, dirty: bool | None = None,
                               runner: Runner | None = None) -> dict[str, Any]:
    from aeos.verification import facade as verification

    if not project.is_platform:
        raise ValidationFailed("only the platform project's own lockfile is scanned here; product repositories are "
                               "not read by AEOS", code="VA_SCAN_PLATFORM_ONLY")
    lock = repo_root() / "requirements.lock"
    if not lock.is_file():
        raise Blocked("requirements.lock not found", code="VA_SCAN_NO_LOCKFILE")
    run = runner or _run
    rc, out, err = run([sys.executable, "-m", "pip_audit", "--version"])
    if rc != 0:
        raise Blocked("pip-audit is not installed; no vulnerability assessment was made", code="VA_SCANNER_MISSING",
                      details={"remedy": INSTALL})
    tool_version = (out or err).strip().splitlines()[0][:80] if (out or err).strip() else "pip-audit"
    rc, out, err = run([sys.executable, "-m", "pip_audit", "-r", str(lock), "--no-deps", "--disable-pip",
                        "--format", "json", "--progress-spinner", "off"])
    try:
        report = json.loads(out) if out.strip() else None
    except ValueError:
        report = None
    if rc not in (0, 1) or not isinstance(report, dict):  # 1 = vulnerabilities found (a valid result)
        raise Blocked(f"pip-audit did not produce a result (exit {rc}): {redact_text(err)[:300]}",
                      code="VA_SCAN_FAILED")
    findings, fixes = _summary(report)
    scanned = len(report.get("dependencies") or [])
    content = {
        "scope": f"Python dependencies of requirements.lock ({scanned} packages)"
                 + (f" at commit {commit[:12]}" if commit else "") + (" (working tree dirty)" if dirty else ""),
        "method": f"{tool_version} against the PyPI/OSV advisory database (--no-deps, pinned lockfile)",
        "tools": "pip-audit",
        "remediation": "; ".join(fixes) if fixes else ("No fix versions named by the advisories."
                                                       if findings else "No known vulnerability reported."),
        "findings": findings, "packages_scanned": scanned, "exit_code": rc,
    }
    ev = verification.collect_evidence(
        session, kind="VA_REPORT", origin="collector", collector_identity=COLLECTOR,
        claim=f"pip-audit of requirements.lock: {len(findings)} advisory finding(s) in {scanned} packages",
        content=content, evidence_level="E2", subject_type="lockfile", subject_id="requirements.lock",
        project_id=project.id, domain=project.domain,
        environment={"commit": commit, "name": "local checkout", "dirty": dirty, "lockfile": "requirements.lock"})
    audit.record(session, actor_type=actor_type, actor_id=actor, action="inventory.va_scan", result="SUCCESS",
                 target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                 details={"evidence_id": ev.id, "findings": len(findings), "packages": scanned})
    return {"evidence_id": ev.id, "findings": len(findings), "packages_scanned": scanned, "fixes": fixes}
