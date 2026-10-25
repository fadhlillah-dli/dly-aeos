"""§27 08./09. and §28 Production validation / Incident documents fed by real AEOS records.

* The 08 Operations documents leave DRAFT only while the newest trusted production evidence validates
  production: the deployment observer's RUNTIME_OBSERVATION of the registered production target
  (delivery.deployment.observe) counts; a staging observation or a newer failed/unattributable observation
  does not. The documents reference the observation (id, target, commit), never copy its verdict.
* 09-incidents carries one INCIDENT_REPORT per OpsIncident that concerns the project; 09-problems one
  PROBLEM_RECORD per evidenced root cause. Nothing is invented: a missing root cause stays pending.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from aeos.docs_engine import renderer as r
from aeos.docs_engine import service
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import ProjectIntegration
from aeos.operations import incident as inc_mod
from tests.unit.test_publisher_support import install_fakes, make_plan, make_project
from tests.unit.test_publisher_support import schema_sample_content as sample

HEAD = "a1b2c3d4e5f6" + "0" * 28
TASKS = [{"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]}]
OPS_DOCS = ("08-operational-overview", "08-monitoring", "08-logs", "08-alerts", "08-slo", "08-backup-restore",
            "08-maintenance")


@pytest.fixture
def world(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db)
    _plan, version = make_plan(db, project, sample(), tasks=TASKS)
    return {"settings": settings, "db": db, "project": project, "version": version}


def _render(world):
    result, _d, _s = service.render_version(world["db"], world["settings"], world["version"], persist=False,
                                            render_visuals=False)
    return result.by_id()


def _section(doc: r.RenderedDocument, key: str) -> str:
    body = doc.body_markdown or ""
    m = re.search(rf"<!-- aeos:section={key} .*?-->\n(.*?)(?=\n## |\Z)", body, re.S)
    return m.group(1) if m else ""


def _target(db, project, purpose: str, url: str) -> None:
    db.add(ProjectIntegration(project_id=project.id, domain=project.domain, provider="deployment-target",
                              purpose=purpose, root_id=url, config={}))
    db.commit()


def _observe(db, project, environment: str, url: str, body: dict) -> dict:
    from aeos.delivery import deployment

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{url}/health").mock(return_value=httpx.Response(200, json=body))
        out = deployment.observe(db, project.id, environment, HEAD, actor="owner")
    db.commit()
    return out


def test_production_observation_of_the_registered_target_validates_the_operations_documents(world):
    db, project = world["db"], world["project"]
    ids = _render(world)
    assert all(ids[k].status == "DRAFT" for k in OPS_DOCS)
    _target(db, project, "staging", "https://staging.example.org")
    _target(db, project, "production", "https://app.example.org")
    from tests.unit.test_deployment_support import make_staging_ready

    make_staging_ready(db, project)  # controlled precondition: staging observations need CAN_VALIDATE_STAGING READY
    staging = _observe(db, project, "staging", "https://staging.example.org", {"status": "ok", "commit": HEAD})
    assert staging["status"] == "PASS"
    ids = _render(world)
    assert ids["08-operational-overview"].status == "DRAFT", "a staging observation is not production validation"

    prod = _observe(db, project, "production", "https://app.example.org", {"status": "ok", "commit": HEAD})
    assert prod["status"] == "PASS"
    ids = _render(world)
    for key in OPS_DOCS:
        assert ids[key].status == "APPROVED", key
        assert prod["evidence_id"] in ids[key].status_reason, key
        assert ids[key].links["evidence"] == [prod["evidence_id"]] and ids[key].links["commit"] == [HEAD]
    overview = _section(ids["08-operational-overview"], "production_architecture")
    assert "https://app.example.org/health" in overview and prod["evidence_id"] in overview
    assert HEAD[:12] in overview
    release = next(d for k, d in ids.items() if k.startswith("07-release-deployment/release-plan-"))
    post = _section(release, "post_release_validation")
    assert prod["evidence_id"] in post and staging["evidence_id"] not in post
    assert not re.search(r"\b(PASS|FAILED|DONE)\b", post), "a LIVE section references evidence, never its verdict"
    assert prod["evidence_id"] in release.links["evidence"]

    # a newer observation that does not validate production returns the documents to DRAFT
    later = _observe(db, project, "production", "https://app.example.org", {"status": "ok", "commit": "f" * 40})
    assert later["status"] == "NOT_PROVEN"
    ids = _render(world)
    assert ids["08-operational-overview"].status == "DRAFT"
    assert later["evidence_id"] in ids["08-operational-overview"].status_reason


def test_the_platform_release_health_validates_the_platform_operations_documents(settings, db, monkeypatch):
    """W6-091: DLY AEOS itself (the first project) has an evidence path - its own /api/health reports the commit
    only under ``controller``; the observer accepts it while the controller runs the ACTIVE clean release, so the
    platform's 08 Operations documents leave DRAFT. A checkout (no promoted release) stays NOT_PROVEN."""
    from aeos.delivery import deployment
    from tests.unit.test_deployment_support import HEALTHY_RELEASE

    install_fakes(monkeypatch)
    project = make_project(db, key="dly-aeos-platform", github_repo="fadhlillah-dli/dly-aeos")
    project.is_platform = True
    db.commit()
    _plan, version = make_plan(db, project, sample(), tasks=TASKS)
    world = {"settings": settings, "db": db, "project": project, "version": version}
    base = "http://127.0.0.1:8787"
    db.add(ProjectIntegration(project_id=project.id, domain=project.domain, provider="deployment-target",
                              purpose="production", root_id=base, config={"health_path": "/api/health"}))
    db.commit()
    checkout = {"status": "ok", "version": "0.1.0", "controller": {
        "mode": "checkout", "release_id": None, "commit": HEAD, "dirty": False, "active_release_id": None,
        "matches_active": None, "status": "NO_ACTIVE_RELEASE"}}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{base}/api/health").mock(return_value=httpx.Response(200, json=checkout))
        before = deployment.observe(db, project.id, "production", HEAD, actor="owner")
    db.commit()
    assert before["status"] == "NOT_PROVEN" and "ACTIVE release" in before["reason"]
    assert _render(world)["08-operational-overview"].status == "DRAFT"
    release = {"status": "ok", "version": "0.1.0", "controller": {**HEALTHY_RELEASE, "commit": HEAD}}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{base}/api/health").mock(return_value=httpx.Response(200, json=release))
        prod = deployment.observe(db, project.id, "production", HEAD, actor="owner")
    db.commit()
    assert prod["status"] == "PASS" and prod["target"] == f"{base}/api/health"
    ids = _render(world)
    for key in OPS_DOCS:
        assert ids[key].status == "APPROVED", key
        assert prod["evidence_id"] in ids[key].status_reason, key


def test_untrusted_or_other_domain_production_evidence_never_validates(world):
    db, project = world["db"], world["project"]
    common = dict(project_id=project.id, kind="RUNTIME_OBSERVATION", content_hash="sha256:x", claim="prod ok",
                  inline={"content": {"status": "PASS", "target": "https://app.example.org/health"}},
                  environment={"commit": HEAD, "deployment_environment": "production"})
    db.add(EvidenceArtifact(origin="worker", collector_identity="w", trusted=True, domain=project.domain, **common))
    db.add(EvidenceArtifact(origin="manual", collector_identity="m", trusted=False, domain=project.domain, **common))
    db.add(EvidenceArtifact(origin="collector", collector_identity="deployment-observer:production", trusted=True,
                            domain="OFFICE", **common))
    db.commit()
    assert _render(world)["08-operational-overview"].status == "DRAFT"


@pytest.mark.parametrize(("status", "approved"), [("FAIL", False), ("NOT_PROVEN", False), ("PASS", True),
                                                  (None, True)])
def test_a_recorded_failed_production_validation_never_approves_the_operations_documents(world, status, approved):
    """§28 "status hanya berdasarkan evidence": a PRODUCTION_VALIDATION artefact approves only when its own result
    is PASS (or states none); one that records FAIL / NOT_PROVEN keeps the documents DRAFT."""
    db, project = world["db"], world["project"]
    content = {"scope": "post-release checks", **({"status": status} if status else {})}
    ev = EvidenceArtifact(project_id=project.id, domain=project.domain, kind="PRODUCTION_VALIDATION",
                          origin="collector", collector_identity="production-validation", trusted=True,
                          content_hash="sha256:pv", claim="post-release validation", inline={"content": content},
                          environment={"commit": HEAD, "name": "production"})
    db.add(ev)
    db.commit()
    doc = _render(world)["08-operational-overview"]
    assert (doc.status == "APPROVED") is approved, (status, doc.status, doc.status_reason)
    if not approved:
        assert ev.id in doc.status_reason and status in doc.status_reason


def _trusted_evidence(db, project) -> str:
    ev = EvidenceArtifact(project_id=project.id, domain=project.domain, kind="LOG", origin="collector",
                          collector_identity="log-collector", trusted=True, content_hash="sha256:log",
                          claim="worker log shows the leaked path", inline={})
    db.add(ev)
    db.commit()
    return ev.id


REGRESSION = "tests/unit/test_operations_incident.py::test_resolve_requires_evidenced_root_cause_action_and_regression"


def test_recorded_incident_becomes_an_incident_report_and_a_problem_record(world):
    db, project = world["db"], world["project"]
    ids = _render(world)
    assert ids["09-incidents"].status == "NOT_STARTED" and ids["09-problems"].status == "NOT_STARTED"

    inc = inc_mod.open_incident(db, title="Worker menulis di luar allowed_paths", actor="owner", owner="Ops Lead",
                                source="manual", domain=project.domain, project_id=project.id,
                                declared_blast_radius="branch aeos/t1")
    other = make_project(db, key="lain")
    inc_mod.open_incident(db, title="Insiden proyek lain", actor="owner", domain="PERSONAL", project_id=other.id)
    inc_mod.open_incident(db, title="Insiden OFFICE", actor="owner", domain="OFFICE")
    db.commit()
    ids = _render(world)
    docs = [d for k, d in ids.items() if k.startswith("09-incidents/")]
    assert len(docs) == 1 and ids["09-incidents"].status == "APPROVED"
    report = docs[0]
    assert report.doc_type == "INCIDENT_REPORT" and report.status == "DRAFT"
    assert "root_cause_with_evidence" in report.status_reason
    assert "Worker menulis di luar allowed_paths" in _section(report, "summary")
    assert "Root Cause" in report.pending_sections, "no evidenced root cause -> pending, never invented"
    assert "Belum diselesaikan" in _section(report, "resolution")
    assert ids["09-problems"].status == "NOT_STARTED", "no evidenced root cause -> no problem record"

    ev = _trusted_evidence(db, project)
    inc_mod.add_timeline(db, inc.id, actor="owner", note="Run dihentikan, branch dibekukan", evidence_ids=[ev])
    inc_mod.record_analysis(db, inc.id, actor="owner", impact="Satu branch kandidat tercemar",
                            root_cause="Glob allowed_paths tidak dinormalisasi", root_cause_evidence_refs=[ev])
    inc_mod.add_corrective_action(db, inc.id, actor="owner", description="Normalisasi glob sebelum dicek")
    inc_mod.link_regression(db, inc.id, actor="owner", test_ids=[REGRESSION])
    inc_mod.resolve(db, inc.id, actor="owner", note="Perbaikan digabung")
    db.commit()
    ids = _render(world)
    report = next(d for k, d in ids.items() if k.startswith("09-incidents/"))
    assert report.status == "APPROVED" and "RESOLVED" in report.status_reason
    assert ev in _section(report, "root_cause") and "Glob allowed_paths" in _section(report, "root_cause")
    assert "Satu branch kandidat tercemar" in _section(report, "impact")
    assert "branch aeos/t1" in _section(report, "impact")
    assert "Run dihentikan" in _section(report, "response")
    assert "Normalisasi glob" in _section(report, "corrective_actions")
    assert REGRESSION in _section(report, "preventive_actions")
    assert "Perbaikan digabung" in _section(report, "resolution")
    for key in ("detection", "timeline", "evidence"):
        assert next(s for s in report.sections if s.key == key).status == r.SEC_LIVE
    assert ev in _section(report, "evidence") and ev in _section(report, "timeline")
    assert report.links["incident"] == [inc.id] and ev in report.links["evidence"]
    assert report.links["regression"] == [REGRESSION]
    assert not report.pending_sections

    problems = [d for k, d in ids.items() if k.startswith("09-problems/")]
    assert len(problems) == 1 and problems[0].doc_type == "PROBLEM_RECORD"
    problem = problems[0]
    assert problem.status == "APPROVED" and inc.id in _section(problem, "problem_statement")
    assert ev in _section(problem, "root_cause") and REGRESSION in _section(problem, "validation")
    # §27 09. Problems: statement / root cause / impact / actions / validation all come from the record
    assert not problem.pending_sections, problem.pending_sections
    assert "Satu branch kandidat tercemar" in _section(problem, "impact")
    assert "Normalisasi glob" in _section(problem, "actions")


def test_a_real_restore_test_reaches_the_platform_backup_document_only(world):
    """§27 08. Backup "restore procedure/testing": the only RESTORE_TEST producer (operations.backup.restore_test)
    records PLATFORM-level evidence (no project, no domain). The platform project's 08-backup-restore names and
    links it; a product project never sees it (it is not that product's restore test)."""
    from aeos.operations import backup

    db, settings, project = world["db"], world["settings"], world["project"]
    bid = backup.create_backup(settings, label="docs", actor="owner")["backup_id"]
    result = backup.restore_test(settings, bid, session=db, actor="owner")
    db.commit()
    assert result["status"] == "PASSED", result
    ev = db.get(EvidenceArtifact, result["evidence_id"])
    assert ev.project_id is None and ev.domain is None and ev.origin == "restore-test"

    product = _render(world)["08-backup-restore"]
    assert ev.id not in (product.body_markdown or "") and ev.id not in product.links.get("evidence", [])
    assert "belum teruji" in _section(product, "restore_testing")

    project.is_platform = True
    db.commit()
    doc = _render(world)["08-backup-restore"]
    assert ev.id in _section(doc, "restore_testing") and ev.id in doc.links["evidence"]
    runbook = _render(world)["08-runbooks/runbook-restart"]
    assert ev.id in runbook.links["evidence"]
    # a restore-test-kind row from any other origin is not this controller's restore test
    db.add(EvidenceArtifact(kind="RESTORE_TEST", origin="collector", collector_identity="x", trusted=True,
                            content_hash="sha256:other", claim="other", inline={}))
    db.commit()
    assert [e.id for e in service.evidence_refs(db, project) if e.kind == "RESTORE_TEST"] == [ev.id]


def test_platform_wide_incident_reaches_the_projects_its_blast_radius_names(world):
    db, project = world["db"], world["project"]
    untouched = make_project(db, key="untouched")
    inc_mod.open_incident(db, title="Emergency stop", actor="owner", source="emergency_stop", source_ref="es-1",
                          status="CONTAINED", domain=None,
                          derived_blast_radius={"PERSONAL": {"projects": [project.id], "runs": ["run-1"]},
                                                "OFFICE": {"projects": ["office-p"], "runs": ["run-o"]}})
    db.commit()
    infos = service.incident_infos(db, project)
    assert [i.title for i in infos] == ["Emergency stop"]
    assert infos[0].derived_blast_radius == ("PERSONAL: proyek 1, run 1, side effect 0, capability -, komponen -",)
    assert service.incident_infos(db, untouched) == []
    report = next(d for k, d in _render(world).items() if k.startswith("09-incidents/"))
    assert "es-1" in _section(report, "detection") and "emergency stop" in _section(report, "detection")
    assert "office-p" not in (report.body_markdown or "")
