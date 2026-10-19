"""The §27/§28 seam: Product 1 documents are rendered from schema-v1 (approved) plan content.

Every approved PlanVersion passes ``planning.service.schema_errors('plan_content', ...)``; these tests use only
content that passes it, so a renderer reading keys the schema forbids is caught here (it was invisible while
every renderer test used a legacy fixture the schema rejects).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from aeos.core.ids import slugify
from aeos.docs_engine import renderer as r
from aeos.docs_engine import visuals as vis
from aeos.docs_engine.plan_content import build_view
from aeos.planning import contracts
from aeos.planning.service import schema_errors
from tests.unit.test_publisher_support import SAMPLE_CONTENT, schema_sample_content

PROJECT = r.ProjectIdentity(id="p-1", key="contoh", name="Contoh App", domain="PERSONAL", owner_name="Owner",
                            repositories=(r.RepoInfo("GITHUB", "owner/contoh", "https://github.com/owner/contoh"),))
VERSION = r.VersionIdentity(id="v-1", plan_id="plan-1", version=1, manifest_hash="sha256:" + "a" * 64, approved=True)

# Execution/planning-meta leaves that are not §27 narrative by design (shown in Planning / Workforce instead).
EXEMPT_PREFIXES = ("schema_version", "facts", "material_questions", "tasks.")
TASK_LEAVES_RENDERED = ("tasks.key", "tasks.title", "tasks.acceptance_criteria")
# Leaves that are references to other leaves (feature/risk keys); their target is checked instead.
REFERENCE_LEAVES = ("requirements.feature_key", "testing.test_plans.feature_keys", "release.release_plans.features",
                    "product_home.active_features", "product_home.open_risks", "features.depends_on",
                    "features.milestone", "tasks.feature_key", "tasks.depends_on",
                    # planning schema rev 2 (cluster C7): governance/contract entries refer to features/milestones/tasks
                    "feature_governance.feature_key", "milestone_governance.milestone_key",
                    "milestone_governance.depends_on", "rfcs.feature_key", "tdds.feature_key",
                    "interface_contracts.feature_keys", "interface_contracts.provider_task_key",
                    "interface_contracts.consumer_task_keys")


def _tasks(content: dict[str, Any]) -> list[r.TaskInfo]:
    """What ``docs_engine.service.task_infos`` reads from the compiled TaskSpecs (feature_key stays raw)."""
    return [r.TaskInfo(id=f"id-{t['key']}", task_key=t["key"], title=t["title"], goal=t["goal"],
                       feature_key=t["feature_key"], acceptance_criteria=tuple(t["acceptance_criteria"]),
                       depends_on=tuple(t["depends_on"])) for t in content.get("tasks", [])]


def _render(content: dict[str, Any], *, tmp_path=None, signals: r.RenderSignals | None = None,
            project: r.ProjectIdentity = PROJECT, version: r.VersionIdentity = VERSION) -> r.RenderResult:
    view = build_view(content)
    visuals = vis.render_visuals(view.diagrams, tmp_path, enabled=False) if tmp_path is not None else []
    return r.render_document_set(project=project, version=version, content=content, tasks=_tasks(content),
                                 visuals=visuals, signals=signals)


def _sections(doc: r.RenderedDocument) -> dict[str, str]:
    return {s.key: s.status for s in doc.sections}


def _section_text(doc: r.RenderedDocument, key: str) -> str:
    body = doc.body_markdown or ""
    m = re.search(rf"<!-- aeos:section={key} [^>]*-->\n(.*?)(?=\n## |\Z)", body, re.S)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------------------------
# A fully filled, schema-valid plan: every string leaf carries a unique token


def _filled_plan() -> tuple[dict[str, Any], dict[str, str]]:
    tokens: dict[str, str] = {}

    def fill(schema: dict[str, Any], path: str) -> Any:
        t = schema.get("type")
        if t == "object":
            return {k: fill(v, f"{path}.{k}" if path else k) for k, v in schema["properties"].items()}
        if t == "array":
            return [fill(schema["items"], path)]
        if t == "integer":
            return 1
        if t == "boolean":
            return True
        if "enum" in schema:
            return schema["enum"][-1] if path != "schema_version" else schema["enum"][0]
        token = f"sv{len(tokens):03d}x"
        tokens[token] = path
        return token

    c = fill(contracts.plan_content_schema(with_header=False), "")
    fkey, mkey, rkey = c["features"][0]["key"], c["milestones"][0]["key"], c["security_privacy"]["initial_risks"][0]["key"]
    c["features"][0].update({"depends_on": [], "milestone": mkey})
    c["requirements"][0]["feature_key"] = fkey
    c["tasks"][0].update({"feature_key": fkey, "depends_on": [], "risk_class": "R3"})
    c["testing"]["test_plans"][0]["feature_keys"] = [fkey]
    c["testing"]["test_plans"][0]["scenarios"][0]["type"] = "functional"
    c["release"]["release_plans"][0]["features"] = [fkey]
    c["product_home"].update({"active_features": [fkey], "open_risks": [rkey], "data_classification": "INTERNAL"})
    c["architecture"]["diagrams"][0]["kind"] = "context"
    c["architecture"]["diagrams"][0]["mermaid"] = f"flowchart TD\n  A[{c['architecture']['diagrams'][0]['mermaid']}]"
    # rev 2 governance sections bound to the filled feature/milestone/task (personal data YES: the DPIA input renders)
    c["feature_governance"][0].update({"feature_key": fkey, "personal_data": "YES"})
    c["milestone_governance"][0].update({"milestone_key": mkey, "depends_on": []})
    c["rfcs"][0]["feature_key"] = fkey
    c["tdds"][0]["feature_key"] = fkey
    tkey = c["tasks"][0]["key"]
    c["interface_contracts"][0].update({"feature_keys": [fkey], "provider_task_key": tkey, "consumer_task_keys": []})
    return c, tokens


def test_schema_fixtures_pass_the_plan_content_schema():
    assert schema_errors("plan_content", schema_sample_content()) == []
    assert schema_errors("plan_content", _filled_plan()[0]) == []


def test_every_schema_leaf_lands_in_a_rendered_document(tmp_path):
    content, tokens = _filled_plan()
    res = _render(content, tmp_path=tmp_path)
    docs = [d for d in res.documents if d.doc_type != "TEMPLATE"]
    text = "\n".join((d.body_markdown or "") + "\n" + d.logical_doc_id for d in docs).lower()
    missing = []
    for token, path in tokens.items():
        if path.startswith(EXEMPT_PREFIXES) and path not in TASK_LEAVES_RENDERED:
            continue
        if path in REFERENCE_LEAVES:
            continue
        if token not in text and slugify(token) not in text:
            missing.append(path)
    assert missing == [], f"schema leaves never rendered into Product 1: {missing}"


def test_filled_schema_plan_leaves_no_core_section_pending(tmp_path):
    content, _tokens = _filled_plan()
    ids = _render(content, tmp_path=tmp_path).by_id()
    # Sections the schema cannot express stay honestly pending; everything else is filled from the plan.
    allowed = {"Objectives"}  # Release Plan objectives are not in the schema
    for lid in ("00-product-home", "01-product-overview", "03-architecture-overview", "04-security-overview",
                "04-security-assessments/initial-risk-assessment", "04-data-inventory", "05-development"):
        assert ids[lid].pending_sections == [], (lid, ids[lid].pending_sections)
    prd = next(d for k, d in ids.items() if k.startswith("02-requirements/prd-"))
    assert prd.pending_sections == [], prd.pending_sections
    tp = next(d for k, d in ids.items() if k.startswith("06-testing/test-plan-"))
    # one functional scenario: regression/performance/security stay pending (the plan names none) — not invented
    assert set(tp.pending_sections) == {"Regression Testing", "Performance Testing", "Security Testing"}
    rel = next(d for k, d in ids.items() if k.startswith("07-release-deployment/release-plan-"))
    assert set(rel.pending_sections) <= allowed, rel.pending_sections


def test_overview_and_architecture_never_dump_a_dict():
    res = _render(schema_sample_content())
    for lid in ("00-product-home", "01-product-overview", "03-architecture-overview",
                "04-security-assessments/initial-risk-assessment"):
        body = res.by_id()[lid].body_markdown
        assert "overview: " not in body and "vision: " not in body and "context: " not in body, lid
    ov = res.by_id()["01-product-overview"]
    assert "Contoh App mencatat dan melacak tugas harian." in _section_text(ov, "overview")
    assert "Tugas tercatat rapi" in _section_text(ov, "vision_objectives")
    assert "Aplikasi mobile" in _section_text(ov, "scope") and "Kebutuhan sinkronisasi" in _section_text(ov, "scope")
    arch = res.by_id()["03-architecture-overview"]
    assert "SPA memanggil REST API lokal." in _section_text(arch, "high_level_architecture")
    assert "/api/tasks" in _section_text(arch, "api_landscape")


def test_product_home_reads_owners_criticality_stack_service_and_risks():
    home = _render(schema_sample_content()).by_id()["00-product-home"]
    owners = _section_text(home, "ownership")
    assert "| Product Owner | Fadhlillah |" in owners and "| Tech Lead | Fadhlillah |" in owners
    assert "| Engineering Lead | belum ditetapkan |" in owners  # UNKNOWN is never shown as a name
    assert "LOW" in _section_text(home, "classification")
    assert "FastAPI" in _section_text(home, "stack") and "contoh-api" in _section_text(home, "repository")
    risks = _section_text(home, "open_risks")
    assert "1 risiko awal" in risks and "`R1`" in risks
    assert "1.0.0" in _section_text(home, "active_work")
    assert _sections(home)["stack"] == r.SEC_FILLED


def test_prd_is_joined_to_requirements_by_feature_key():
    prd = _render(schema_sample_content()).by_id()["02-requirements/prd-f1"]
    secs = _sections(prd)
    for key in ("problem", "background", "goals_non_goals", "business_requirements", "non_functional_requirements",
                "user_stories", "user_customer_impact", "assumptions", "risks", "success_metrics",
                "rollout_considerations"):
        assert secs[key] == r.SEC_FILLED, key
    assert "Respons < 300 ms" in _section_text(prd, "non_functional_requirements")
    ac = _section_text(prd, "acceptance_criteria")
    assert "User dapat membuat tugas" in ac and "Tugas tersimpan setelah reload" in ac
    assert "T1 works" in ac  # TaskSpec feature_key "F1" matches the document key "f1"
    assert "SQLite lokal" in _section_text(prd, "dependencies")
    assert prd.links["tasks"] == ["T1"]


def test_initial_risk_register_threat_model_and_data_inventory_come_from_security_privacy():
    ids = _render(schema_sample_content()).by_id()
    ra = ids["04-security-assessments/initial-risk-assessment"]
    reg = _section_text(ra, "risk_register")
    assert "| R1 | Kebocoran data — File DB tersalin | LOW | HIGH | Fadhlillah |" in reg
    assert "R1: Enkripsi disk" in _section_text(ra, "mitigations")
    assert "Rendah setelah enkripsi" in _section_text(ra, "residual_risk")
    assert _sections(ra)["residual_risk"] == r.SEC_HUMAN  # a proposal is shown, never accepted by AEOS
    assert "Perlu backup terenkripsi?" in _section_text(ra, "context_scope")
    tm = ids["04-threat-models/threat-model-initial"]
    assert ids["04-threat-models"].status == "APPROVED"
    assert "Credential stuffing" in _section_text(tm, "threats")
    assert "Tebak password berulang" in _section_text(tm, "attack_scenarios")
    assert "Rate limit" in _section_text(tm, "mitigations")
    inv = ids["04-data-inventory"]
    assert "| Tugas | INTERNAL | Fadhlillah | SQLite | Pemilik | Sampai dihapus | Hapus file DB |" in \
        _section_text(inv, "data_types")
    assert inv.pending_sections == []
    sec = ids["04-security-overview"]
    assert "Password Argon2id" in _section_text(sec, "authn_authz") and "Fadhlillah" in _section_text(sec, "ownership")
    dev = ids["05-development"]
    assert dev.pending_sections == [] and "Tidak di repo" in _section_text(dev, "secrets")


def test_risky_task_class_requires_a_per_change_security_assessment():
    ids = _render(schema_sample_content()).by_id()
    assert "04-security-assessments/security-assessment-f2" in ids  # T2 is R3
    assert "04-security-assessments/security-assessment-f1" not in ids  # T1 is R1
    sa = ids["04-security-assessments/security-assessment-f2"]
    assert "TaskSpec `T2`" in _section_text(sa, "scope") and "R3" in _section_text(sa, "scope")
    assert "CSRF" in _section_text(sa, "requirements")
    assert _sections(sa)["findings"] == r.SEC_LIVE and _sections(sa)["approval"] == r.SEC_HUMAN


def test_prd_rfc_reference_creates_the_rfc_document():
    ids = _render(schema_sample_content()).by_id()
    assert "03-rfcs/rfc-f1" in ids and ids["03-rfcs"].status == "APPROVED"
    assert "RFC: Penyimpanan tugas lokal" in ids["03-rfcs/rfc-f1"].body_markdown
    assert "03-rfcs/rfc-f1" in ids["02-requirements/prd-f1"].links["related"]
    assert ids["03-tdds"].status == "NOT_STARTED"


def test_test_plans_follow_the_schema_and_uncovered_features_keep_a_derived_plan():
    ids = _render(schema_sample_content()).by_id()
    tp = ids["06-testing/test-plan-tp1"]
    assert set(tp.pending_sections) == {"Regression Testing", "Performance Testing", "Security Testing"}
    assert "Risk-based" in _section_text(tp, "approach") and "pytest" in _section_text(tp, "tools")
    assert "**S1** — Buat tugas (functional)" in _section_text(tp, "functional_testing")
    assert "Uji penerimaan" in _section_text(tp, "uat") and _sections(tp)["uat"] == r.SEC_LIVE
    assert "06-testing/test-plan-f2" in ids  # F2 is not covered by TP1
    assert "06-testing/test-plan-f1" not in ids
    prd = ids["02-requirements/prd-f1"]
    assert prd.links["related"][-1] == "06-testing/test-plan-tp1"


def test_release_plan_is_rendered_per_version_with_release_notes():
    ids = _render(schema_sample_content()).by_id()
    assert ids["07-release-deployment"].status == "APPROVED"
    rel = ids["07-release-deployment/release-plan-1-0-0"]
    assert rel.doc_type == "RELEASE_PLAN" and rel.timing == "RELEASE_CANDIDATE"
    assert "MVP pencatatan tugas" in _section_text(rel, "release_notes")
    assert "Install versi sebelumnya" in _section_text(rel, "rollback")
    secs = _sections(rel)
    for key in ("testing_status", "security_status", "uat_status", "production_readiness", "post_release_validation"):
        assert secs[key] == r.SEC_LIVE, key
    assert secs["sign_off"] == r.SEC_HUMAN
    assert "Kelola tugas (`f1`)" in _section_text(rel, "changes")


def test_operations_are_draft_proposals_until_production_validation_evidence():
    content = schema_sample_content()
    ids = _render(content).by_id()
    for lid in ("08-operational-overview", "08-monitoring", "08-logs", "08-alerts", "08-slo", "08-backup-restore",
                "08-maintenance"):
        d = ids[lid]
        assert d.status == "DRAFT" and "production validation" in d.status_reason, lid
        assert "Usulan dari rencana" in d.body_markdown
    assert "Vacuum SQLite bulanan" in ids["08-maintenance"].body_markdown
    assert "99%" in _section_text(ids["08-slo"], "availability")
    backup = ids["08-backup-restore"]
    assert _sections(backup)["restore_testing"] == r.SEC_LIVE
    assert "belum teruji" in _section_text(backup, "restore_testing")
    rb = ids["08-runbooks/runbook-restart"]
    assert "Pulihkan server lokal" in _section_text(rb, "purpose_trigger") and "Procedure" in rb.pending_sections
    ev = r.EvidenceRef(id="ev-prod-1", kind="PRODUCTION_VALIDATION", origin="collector", commit="c" * 40)
    validated = _render(content, signals=r.RenderSignals(evidence=[ev])).by_id()["08-operational-overview"]
    assert validated.status == "APPROVED" and "ev-prod-1" in validated.status_reason


def test_dynamic_sections_never_copy_a_status_snapshot():
    for content in (schema_sample_content(), copy.deepcopy(SAMPLE_CONTENT)):
        res = _render(content)
        for d in res.documents:
            if d.kind != "DOC" or d.doc_type == "TEMPLATE" or d.status in ("NOT_STARTED", "NOT_APPLICABLE"):
                continue
            for s in d.sections:
                if s.status != r.SEC_LIVE:
                    continue
                text = _section_text(d, s.key)
                assert not re.search(r"\b(NOT STARTED|PASS|FAILED|DONE|Belum ada eksekusi|belum dilakukan)\b", text), \
                    (d.logical_doc_id, s.key, text)
        tp = next(d for d in res.documents if d.doc_type == "TEST_PLAN")
        assert "| Hasil |" not in (tp.body_markdown or "")


def test_unknown_values_are_absent_not_names():
    c = schema_sample_content()
    c["product_home"]["product_owner"] = "UNKNOWN"
    c["security_privacy"]["overview"]["ownership"] = "unknown"
    ids = _render(c).by_id()
    assert "UNKNOWN (Product Owner)" not in ids["00-product-home"].body_markdown
    assert "| Product Owner | belum ditetapkan |" in _section_text(ids["00-product-home"], "ownership")
    assert "unknown (Security Lead" not in ids["04-security-overview"].body_markdown


def test_prd_without_a_feature_entry_is_kept():
    c = schema_sample_content()
    c["requirements"].append({**c["requirements"][0], "feature_key": "F9", "title": "PRD Ekspor"})
    ids = _render(c).by_id()
    assert "02-requirements/prd-f9" in ids
    assert "tidak dibuang" in ids["02-requirements/prd-f9"].body_markdown


def test_va_and_pt_documents_exist_only_for_trusted_result_evidence():
    c = schema_sample_content()
    ids = _render(c).by_id()
    assert ids["04-vulnerability-assessments"].status == "NOT_STARTED"
    va = r.EvidenceRef(id="ev-va-123456789", kind="VA_REPORT", claim="Scan dependensi", origin="collector",
                       evidence_level="E3", commit="d" * 40, summary=(("scope", "API lokal"), ("method", "pip-audit")))
    pt = r.EvidenceRef(id="ev-pt-123456789", kind="PT_REPORT", claim="Pentest login", origin="collector")
    ids = _render(c, signals=r.RenderSignals(evidence=[va, pt])).by_id()
    vdoc = next(d for k, d in ids.items() if k.startswith("04-vulnerability-assessments/va-"))
    assert "API lokal" in _section_text(vdoc, "scope") and "pip-audit" in _section_text(vdoc, "method_tools")
    assert _sections(vdoc)["findings"] == r.SEC_LIVE and "ev-va-123456789" in _section_text(vdoc, "findings")
    pdoc = next(d for k, d in ids.items() if k.startswith("04-penetration-tests/pt-"))
    assert _sections(pdoc)["rules_of_engagement"] == r.SEC_HUMAN


def test_screenshots_are_referenced_only_from_trusted_evidence_at_a_commit():
    shot = r.EvidenceRef(id="ev-shot-1", kind="SCREENSHOT", claim="Dashboard", origin="collector", commit="9" * 40)
    home = _render(schema_sample_content(), signals=r.RenderSignals(evidence=[shot])).by_id()["00-product-home"]
    assert "ev-shot-1" in _section_text(home, "quick_links") and _sections(home)["quick_links"] == r.SEC_LIVE
    assert vis.screenshot_manifest(project_id="p", commit=None, environment=None)["status"] == "NOT_PROVEN"
    worker = {"id": "w", "kind": "SCREENSHOT", "trusted": True, "origin": "worker", "environment": {"commit": "1" * 40}}
    assert vis.screenshot_manifest(project_id="p", commit=None, environment=None, evidence=[worker])["status"] == \
        "NOT_PROVEN"
    ok = {**worker, "id": "c", "origin": "collector"}
    man = vis.screenshot_manifest(project_id="p", commit=None, environment=None, evidence=[ok])
    assert man["status"] == "EVIDENCE_REFERENCED" and man["items"][0]["evidence_id"] == "c"


def _change(**kw: Any) -> r.ChangeInfo:
    base = dict(task_id="id-T1", task_key="T1", title="Task T1", goal="Implement T1", feature_key="F1",
                risk_class="R1", acceptance_criteria=("T1 works",), allowed_paths=("src/t1/**",), pr_id="pr-1",
                pr_number=7, pr_url="https://github.com/owner/contoh/pull/7", pr_state="OPEN", repo="owner/contoh",
                head_ref="aeos/t1", base_ref="main", accepted_head_sha="a" * 40, acceptance_id="acc-1",
                acceptance_status="PENDING", evidence_ids=("ev-1",), bundle_evidence_id="ev-bundle",
                rollback_notes=("Revert merge commit lewat PR baru.",), contract_deltas=("backend/aeos/x/facade.py",))
    base.update(kw)
    return r.ChangeInfo(**base)


def test_change_record_per_pr_is_draft_until_accepted_and_links_commits():
    c = schema_sample_content()
    ids = _render(c, signals=r.RenderSignals(changes=[_change()])).by_id()
    ch = ids["10-changes/change-t1"]
    assert ids["10-changes"].status == "APPROVED"
    assert ch.status == "DRAFT" and "PENDING" in ch.status_reason
    assert "backend/aeos/x/facade.py" in _section_text(ch, "scope")
    assert "Revert merge commit" in _section_text(ch, "rollback")
    assert _sections(ch)["approval"] == r.SEC_HUMAN and _sections(ch)["implementation"] == r.SEC_LIVE
    merged = _change(acceptance_status="ACCEPTED", merge_commit_sha="m" * 40, merged=True)
    ids = _render(c, signals=r.RenderSignals(changes=[merged])).by_id()
    assert ids["10-changes/change-t1"].status == "APPROVED"
    prd = ids["02-requirements/prd-f1"]
    assert prd.links["accepted_head"] == ["a" * 40] and prd.links["merge_commit"] == ["m" * 40]
    assert ids["06-testing/test-plan-tp1"].links["merge_commit"] == ["m" * 40]
    assert "10-changes/change-t1" in _section_text(prd, "related_work")


def test_improvements_come_from_project_change_proposals():
    imp = r.ImprovementInfo(id="prop-123456789", title="Cache daftar tugas", kind="ADD", status="SUBMITTED",
                            problem="Daftar lambat", expected_benefit="Lebih cepat", owner="Fadhlillah")
    ids = _render(schema_sample_content(), signals=r.RenderSignals(improvements=[imp])).by_id()
    doc = next(d for k, d in ids.items() if k.startswith("11-improvements/improvement-"))
    assert "Lebih cepat" in _section_text(doc, "expected_benefit")
    assert _sections(doc)["result"] == r.SEC_LIVE and "SUBMITTED" not in doc.body_markdown


def test_retirement_plan_carries_the_closure_checklist_when_retiring():
    checklist = {"items": [
        {"key": "consumers", "title": "Consumer dan dependency", "status": "REVIEW_REQUIRED",
         "detail": {"repositories": ["owner/contoh"], "integrations": []}, "owner_action": "Petakan consumer"},
        {"key": "data", "title": "Retensi dan disposal data", "status": "OWNER_DECISION_REQUIRED",
         "detail": {"attachments": 3}, "owner_action": "Tetapkan retensi"},
        {"key": "final_verification", "title": "Verifikasi akhir pemilik", "status": "PENDING_OWNER", "detail": {},
         "owner_action": "Catat keputusan RETIREMENT"}]}
    retiring = r.ProjectIdentity(id="p-1", key="contoh", name="Contoh App", domain="PERSONAL", lifecycle="RETIRING")
    doc = _render(schema_sample_content(), project=retiring,
                  signals=r.RenderSignals(retirement=checklist)).by_id()["12-retirement"]
    assert doc.status == "APPROVED"
    assert "owner/contoh" in _section_text(doc, "integration_closure")
    assert "Tetapkan retensi" in _section_text(doc, "data_closure")
    assert "Hapus file DB" in _section_text(doc, "privacy_closure")
    assert _sections(doc)["final_approval"] == r.SEC_HUMAN and _sections(doc)["execution"] == r.SEC_LIVE
    assert "REVIEW_REQUIRED" not in doc.body_markdown  # live checklist status is referenced, not copied


def test_diagram_verification_source_and_checks_are_shown(tmp_path):
    c = schema_sample_content()
    c["architecture"]["diagrams"] += [
        {"key": "erd", "title": "ERD", "kind": "erd", "mermaid": "erDiagram\n  tasks ||--o{ ghost_table : has",
         "verification_source": "migrations"},
        {"key": "tb", "title": "Trust", "kind": "trust_boundary", "mermaid": "flowchart LR\n  A[Internet] --> B[API]",
         "verification_source": "threat model"}]
    view = build_view(c)
    visuals = vis.render_visuals(view.diagrams, tmp_path, enabled=False)
    vis.verify_visuals(visuals, trust_boundaries=["Browser"], known_tables={"tasks"})
    by_key = {v.key: v for v in visuals}
    assert by_key["context"].verification_status == "UNVERIFIED"
    assert by_key["context"].verification_source == "contract API"
    assert by_key["erd"].verification_status == "MISMATCH" and "ghost_table" in by_key["erd"].verification_notes[0]
    assert by_key["tb"].verification_status == "MISMATCH" and "Browser" in by_key["tb"].verification_notes[0]
    arch = r.render_document_set(project=PROJECT, version=VERSION, content=c, visuals=visuals).by_id()[
        "03-architecture-overview"]
    assert "Verifikasi diagram: **MISMATCH**" in arch.body_markdown
    assert "periksa terhadap contract API" in arch.body_markdown


@pytest.mark.parametrize("raw", ["F1", "f1", " F1 "])
def test_task_feature_key_matches_the_document_key(raw):
    c = schema_sample_content()
    tasks = [r.TaskInfo(id="x", task_key="T1", title="t", feature_key=raw, acceptance_criteria=("ac-x",))]
    prd = r.render_document_set(project=PROJECT, version=VERSION, content=c, tasks=tasks).by_id()[
        "02-requirements/prd-f1"]
    assert "ac-x" in _section_text(prd, "acceptance_criteria")
