"""PRODUCT 1 structure completeness (master §27, §28, U-08)."""

from __future__ import annotations

import re

from aeos.core.enums import ContentType
from aeos.docs_engine import product1 as p1

# Every section named in the §27 table, per document type (case-insensitive substring match on titles).
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "PRODUCT_HOME": ["Identity", "Product Owner", "Engineering Lead", "Tech Lead", "Repository", "Issue Tracker",
                     "Service", "Criticality", "Data Classification", "Stack", "Quick Links", "Current Development",
                     "Active Epics", "Features", "Release", "Open Risks", "Security Findings", "SDLC Lifecycle",
                     "Governance Links"],
    "PRODUCT_OVERVIEW": ["Overview", "Vision", "Objectives", "Scope", "Team", "Ownership", "Stakeholders", "Roadmap",
                         "System / Service Inventory", "Data Overview"],
    "PRD": ["Overview", "Problem", "Background", "Goals", "Non-goals", "Business Requirements",
            "Functional Requirements", "Non-functional Requirements", "User Stories", "Acceptance Criteria",
            "User / Customer Impact", "Dependencies", "Assumptions", "Risks", "Success Metrics",
            "Rollout Considerations", "Related Work Items", "RFC", "TDD"],
    "ARCHITECTURE_OVERVIEW": ["Context", "High-level", "Component", "Data Architecture", "Data Flow",
                              "External Integrations", "API Landscape", "Infrastructure", "Security Architecture",
                              "Principles", "Decisions"],
    "RFC": ["Context", "Problem", "Proposal", "Goals", "Non-goals", "Alternatives", "Architecture Impact",
            "Security Impact", "Privacy Impact", "Operations Impact", "Performance Impact", "Risks", "Dependencies",
            "Migration", "Rollback", "Decision", "Approval"],
    "TDD": ["Components", "Data Model", "Data Flow", "API", "Integration", "Authn", "Authz", "Controls", "Errors",
            "Logs", "Monitoring", "Performance", "Deployment", "Migration", "Rollback", "Dependencies",
            "Technical Risks", "Testing"],
    "SECURITY_OVERVIEW": ["Ownership", "Authn", "Authz", "Encryption", "Secrets", "Logs", "Monitoring", "Controls"],
    "SECURITY_ASSESSMENT": ["Scope", "Requirements", "Risks", "Findings", "Mitigations", "Residual Risk", "Approval"],
    "THREAT_MODEL": ["Assets", "Actors", "Boundaries", "Entry Points", "Threats", "Attack Scenarios", "Mitigations"],
    "DATA_INVENTORY": ["Data Types", "Classification", "Owner", "Storage", "Access", "Retention", "Disposal"],
    "DEVELOPMENT_GUIDELINES": ["Development Guidelines", "Coding", "Secure Coding", "Branching", "PR", "Review",
                               "Dependency", "Open-source", "Dev Environment", "Secrets", "CI/CD"],
    "TEST_PLAN": ["Overview", "Objectives", "Scope", "Test Strategy", "Approach", "Levels", "Types", "Environment",
                  "Data", "Tools", "Scenarios", "Cases", "Functional", "Regression", "Performance", "Security", "UAT",
                  "Execution", "Results", "Defect Management", "Exit Criteria", "Final Assessment", "Sign-off",
                  "Evidence", "References"],
    "RELEASE_PLAN": ["Overview", "Objectives", "Scope", "Features", "Enhancements", "Fixes", "Breaking Changes",
                     "Related Requirements", "Architecture", "Testing", "Security", "UAT", "Known Issues", "Risks",
                     "Release Notes", "Deployment", "Migration", "Rollback", "Production Readiness", "Sign-off",
                     "Post-release Validation", "Evidence"],
    "OPERATIONAL_OVERVIEW": ["Production Architecture", "Ownership", "Dependencies", "Support", "Criticality", "SLI",
                             "SLO", "SLA"],
    "MONITORING": ["Metrics", "Dashboards", "Coverage", "Thresholds"],
    "LOGGING": ["Application Logs", "Audit Logs", "Security Logs", "Retention", "Access"],
    "ALERTING": ["Alert Rules", "Severity", "Notification", "Escalation"],
    "SLO": ["Availability", "Latency", "Error"],
    "BACKUP_RESTORE": ["Backup Scope", "Frequency", "Retention", "Storage", "Restore Procedure", "Restore Testing"],
    "MAINTENANCE": ["Jobs", "Checks", "Routine Activities"],
    "INCIDENT_REPORT": ["Summary", "Impact", "Detection", "Timeline", "Response", "Root Cause", "Resolution",
                        "Corrective", "Preventive", "Evidence"],
    "PROBLEM_RECORD": ["Problem Statement", "Root Cause", "Impact", "Actions", "Validation"],
    "CHANGE_RECORD": ["Description", "Reason", "Scope", "Risk", "Impact", "Implementation", "Testing", "Approval",
                      "Rollback", "Validation", "Evidence"],
    "IMPROVEMENT_RECORD": ["Opportunity", "Current State", "Proposal", "Expected Benefit", "Risk", "Owner",
                           "Implementation", "Result", "Metrics"],
    "RETIREMENT_PLAN": ["Overview", "Scope", "Impact", "Data", "Access", "Integration", "Infrastructure", "Security",
                        "Privacy", "Backup", "Communication", "Execution", "Validation", "Evidence", "Final Approval"],
}


def _titles(doc_type: str) -> str:
    return " | ".join(s.title for s in p1.DOC_TYPES[doc_type].sections).lower()


def test_title_and_outer_folder():
    assert p1.product_title("Contoh") == "PRODUCT 1 (Contoh) — SOFTWARE DEVELOPMENT & ENGINEERING"
    s = p1.structure()
    assert s["title_template"].startswith("PRODUCT 1 (<Nama Produk>)")
    assert s["root"]["kind"] == "FOLDER" and "nama proyek" in s["root"]["title"]


def test_all_thirteen_nodes_and_templates_present_in_order():
    numbers = [n.number for n in p1.TREE]
    assert numbers == ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "TPL"]
    titles = [n.title for n in p1.TREE]
    assert titles[0] == "00. Product Home" and titles[-2] == "12. Retirement" and titles[-1] == "Templates"


def test_every_section_named_in_section_27_is_present():
    for doc_type, names in REQUIRED_SECTIONS.items():
        titles = _titles(doc_type)
        for name in names:
            assert name.lower() in titles, f"{doc_type} is missing section {name!r}"


def test_product_home_and_overview_are_single_documents_not_subfolders():
    home, overview = p1.node_by_key("00-product-home"), p1.node_by_key("01-product-overview")
    for n in (home, overview):
        assert n.kind == p1.NodeKind.DOCUMENT and n.children == () and n.collection is None
    assert len(p1.DOC_TYPES["PRODUCT_HOME"].sections) >= 10  # headings are sections, not pages


def test_test_strategy_is_a_section_of_the_test_plan_and_release_notes_of_the_release_plan():
    assert "test_strategy" in [s.key for s in p1.DOC_TYPES["TEST_PLAN"].sections]
    assert "release_notes" in [s.key for s in p1.DOC_TYPES["RELEASE_PLAN"].sections]
    all_titles = [n.title.lower() for n, _ in p1.iter_nodes()]
    assert not any("test strategy" in t for t in all_titles)
    assert not any("release notes" in t for t in all_titles)
    assert "TEST_STRATEGY" not in p1.DOC_TYPES and "RELEASE_NOTES" not in p1.DOC_TYPES


def test_folder_vs_document_per_master():
    folders = {n.key for n, _ in p1.iter_nodes() if n.kind == p1.NodeKind.FOLDER}
    docs = {n.key for n, _ in p1.iter_nodes() if n.kind == p1.NodeKind.DOCUMENT}
    assert {"02-requirements", "03-architecture-design", "03-rfcs", "03-tdds", "04-security-privacy",
            "06-testing", "07-release-deployment", "08-operations", "08-runbooks", "09-incidents-problems",
            "10-changes", "11-improvements", "templates"} <= folders
    assert {"00-product-home", "01-product-overview", "03-architecture-overview", "04-security-overview",
            "04-data-inventory", "05-development", "08-operational-overview", "12-retirement"} <= docs
    assert p1.node_by_key("02-requirements").collection == "PRD"
    assert p1.node_by_key("02-requirements").collection_per == "feature"
    assert p1.node_by_key("06-testing").collection == "TEST_PLAN"
    assert p1.node_by_key("07-release-deployment").collection == "RELEASE_PLAN"
    assert p1.node_by_key("07-release-deployment").collection_per == "release"
    sec = {c.key for c in p1.node_by_key("04-security-privacy").children}
    assert {"04-security-assessments", "04-threat-models", "04-vulnerability-assessments", "04-penetration-tests",
            "04-privacy-assessments", "04-data-inventory", "04-security-privacy-decisions"} <= sec
    ops = {c.key for c in p1.node_by_key("08-operations").children}
    assert {"08-operational-overview", "08-runbooks", "08-monitoring", "08-logs", "08-alerts", "08-slo",
            "08-backup-restore", "08-maintenance"} == ops


def test_exactly_the_twelve_templates():
    assert [t.title for t in p1.TEMPLATES] == [
        "PRD", "RFC", "TDD", "Risk Assessment", "Security Assessment", "Threat Model", "Privacy Assessment / DPIA",
        "Test Plan", "Release Plan", "Runbook", "Post-Mortem", "Retirement & Decommission"]


def test_template_files_exist_match_spec_and_carry_no_tenant_data():
    for t in p1.TEMPLATES:
        path = p1.templates_dir() / f"{t.key}.md"
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        assert text == p1.template_from_spec(t.key), f"{path.name} drifted from the spec; regenerate"
        headings = re.findall(r"^## (.+)$", text, flags=re.M)
        assert headings == [s.title for s in p1.DOC_TYPES[t.doc_type].sections]
        assert not re.search(r"(?i)(password|api[_-]?key|token\s*[:=]|@[a-z0-9-]+\.(com|id)|atlassian\.net)", text)


def test_every_section_has_a_content_type_and_human_sections_exist_for_approvals():
    for spec in p1.DOC_TYPES.values():
        for s in spec.sections:
            assert isinstance(s.content_type, ContentType)
    for dt, key in (("RFC", "decision_approval"), ("TEST_PLAN", "sign_off"), ("RELEASE_PLAN", "sign_off"),
                    ("SECURITY_ASSESSMENT", "approval"), ("RISK_ASSESSMENT", "acceptance"),
                    ("RETIREMENT_PLAN", "final_approval"), ("PRODUCT_HOME", "governance_links")):
        assert p1.DOC_TYPES[dt].section(key).content_type == ContentType.HUMAN_GOVERNED
    # results that must come from evidence are DYNAMIC, never AI narrative
    for dt, key in (("TEST_PLAN", "execution_results"), ("TEST_PLAN", "uat"), ("PENETRATION_TEST", "findings"),
                    ("VULNERABILITY_ASSESSMENT", "findings"), ("BACKUP_RESTORE", "restore_testing")):
        assert p1.DOC_TYPES[dt].section(key).content_type == ContentType.DYNAMIC


def test_timing_table_matches_section_28():
    timings = [r["timing"] for r in p1.TIMING_TABLE]
    assert timings == ["PROJECT_CREATION", "FEATURE_APPROVAL", "IMPLEMENTATION_PR", "RELEASE_CANDIDATE",
                       "PRODUCTION_VALIDATION", "INCIDENT", "RETIREMENT"]
    assert p1.DOC_TYPES["PRODUCT_HOME"].timing == p1.Timing.PROJECT_CREATION
    assert p1.DOC_TYPES["TEST_PLAN"].timing == p1.Timing.FEATURE_APPROVAL
    assert p1.DOC_TYPES["RELEASE_PLAN"].timing == p1.Timing.RELEASE_CANDIDATE
    assert p1.DOC_TYPES["RETIREMENT_PLAN"].timing == p1.Timing.RETIREMENT


def test_structure_json_is_serialisable():
    import json

    s = p1.structure()
    json.dumps(s)
    assert len(s["templates"]) == 12 and len(s["nodes"]) == 14
