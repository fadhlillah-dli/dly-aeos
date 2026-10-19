"""PRODUCT 1 — SOFTWARE DEVELOPMENT & ENGINEERING structure (master §27, §28, U-08).

This module is the single code source for the Product 1 tree. It distinguishes, exactly as the
master does:

* **folders** (Drive folder / Confluence folder) — only for nodes that hold several documents;
* **documents** (Google Doc / Confluence page);
* **sections inside a document** — headings, never sub-pages. Product Home and Product Overview are
  single documents with sections (§27 "bukan subfolder untuk setiap heading"); Test Strategy is a
  section of the Test Plan and Release Notes a section of the Release Plan (U-08).

Every section carries a §28 content type:

* ``STATIC`` — identity facts (project id/name/domain, repository, classification);
* ``DYNAMIC`` — ticket/release/test/evidence status read from the live source (GitHub Issues /
  Jira / verifier / CI). Documents *reference* the source; they never copy a status snapshot;
* ``AI_MAINTAINED`` — narrative proposal from the approved plan (planning output is DATA);
* ``HUMAN_GOVERNED`` — decisions, risk acceptance, approval, sign-off. AEOS never fills these.

Timing (§28) says when a document is created/updated. Nodes whose time has not come are rendered as
NOT STARTED / NOT APPLICABLE with a reason and the accountable owner — never with invented PT/VA,
UAT, restore-test results or approvals (§27).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from aeos.core.enums import ContentType
from aeos.core.paths import repo_root

PRODUCT_TITLE_TEMPLATE = "PRODUCT 1 ({name}) — SOFTWARE DEVELOPMENT & ENGINEERING"
STRUCTURE_VERSION = "1"

S = ContentType.STATIC
D = ContentType.DYNAMIC
A = ContentType.AI_MAINTAINED
H = ContentType.HUMAN_GOVERNED


class NodeKind(StrEnum):
    FOLDER = "FOLDER"
    DOCUMENT = "DOCUMENT"


class Timing(StrEnum):
    """§28 'Kapan dokumen dibuat dan diperbarui'."""

    PROJECT_CREATION = "PROJECT_CREATION"
    FEATURE_APPROVAL = "FEATURE_APPROVAL"
    IMPLEMENTATION_PR = "IMPLEMENTATION_PR"
    RELEASE_CANDIDATE = "RELEASE_CANDIDATE"
    PRODUCTION_VALIDATION = "PRODUCTION_VALIDATION"
    INCIDENT = "INCIDENT"
    RETIREMENT = "RETIREMENT"
    STATIC_TEMPLATE = "STATIC_TEMPLATE"


TIMING_TABLE: list[dict[str, str]] = [
    {"timing": Timing.PROJECT_CREATION, "documents": "Product Home, Overview, initial PRD, Architecture Overview, "
     "initial risk/security, development baseline, skeleton lifecycle"},
    {"timing": Timing.FEATURE_APPROVAL, "documents": "Detail PRD/RFC/TDD bila diperlukan, API/schema/design contract, "
     "threat/assessment, Test Plan dan AC"},
    {"timing": Timing.IMPLEMENTATION_PR, "documents": "Delta kontrak, keputusan dan perilaku yang berubah; machine "
     "references dan test evidence; tidak menulis ulang seluruh handbook"},
    {"timing": Timing.RELEASE_CANDIDATE, "documents": "Snapshot konsisten, Release Plan termasuk Release Notes, "
     "migration/rollback/readiness, runbook sesuai scope"},
    {"timing": Timing.PRODUCTION_VALIDATION, "documents": "Referensi deployment, post-release checks, monitoring, "
     "operational ownership; status hanya berdasarkan evidence"},
    {"timing": Timing.INCIDENT, "documents": "Timeline dan evidence, analisis/keputusan, corrective action; "
     "kegagalan menjadi regression/eval case"},
    {"timing": Timing.RETIREMENT, "documents": "Plan closure dan evidence eksekusi ketika lifecycle benar-benar retirement"},
]

CONTENT_TYPE_DESCRIPTIONS: dict[str, str] = {
    S: "Identitas (project, domain, repository, klasifikasi) — fakta dari record AEOS.",
    D: "Status tiket/release/test dari API sumber — dokumen merujuk sumber live, tidak menyalin status.",
    A: "Narasi proposal dari PlanVersion yang disetujui — AI tidak menentukan status dinamis atau approval.",
    H: "Keputusan, risk acceptance, approval, sign-off — diisi manusia yang berwenang, tidak pernah oleh AEOS.",
}


@dataclass(frozen=True)
class SectionSpec:
    key: str
    title: str
    content_type: ContentType
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "content_type": self.content_type.value, "note": self.note}


@dataclass(frozen=True)
class DocTypeSpec:
    """A kind of document (PRD, Test Plan, ...): its sections and template."""

    doc_type: str
    title: str
    sections: tuple[SectionSpec, ...]
    template_key: str | None = None
    timing: Timing = Timing.PROJECT_CREATION
    owner_role: str = "Product Owner"
    description: str = ""

    def section(self, key: str) -> SectionSpec:
        for s in self.sections:
            if s.key == key:
                return s
        raise KeyError(f"{self.doc_type} has no section {key!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "title": self.title,
            "template_key": self.template_key,
            "timing": self.timing.value,
            "owner_role": self.owner_role,
            "description": self.description,
            "sections": [s.to_dict() for s in self.sections],
        }


@dataclass(frozen=True)
class Node:
    """A node of the Product 1 tree: a folder or a document.

    ``doc_type`` set + ``kind=DOCUMENT`` → a single document at this position.
    ``kind=FOLDER`` + ``collection`` → a folder holding one document of ``collection`` per item
    (feature/release/incident/...); ``children`` are fixed sub-nodes.
    """

    key: str  # logical key segment, stable: "00-product-home", "02-requirements", ...
    number: str  # "00".."12" | "TPL"
    title: str
    kind: NodeKind
    doc_type: str | None = None
    collection: str | None = None  # doc_type for per-item documents inside this folder
    collection_per: str = ""  # "feature" | "release" | "incident" | ...
    children: tuple[Node, ...] = ()
    timing: Timing = Timing.PROJECT_CREATION
    owner_role: str = "Product Owner"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "number": self.number,
            "title": self.title,
            "kind": self.kind.value,
            "doc_type": self.doc_type,
            "collection": self.collection,
            "collection_per": self.collection_per,
            "timing": self.timing.value,
            "owner_role": self.owner_role,
            "description": self.description,
            "sections": [s.to_dict() for s in DOC_TYPES[self.doc_type].sections] if self.doc_type else [],
            "children": [c.to_dict() for c in self.children],
        }


def _secs(*items: tuple[str, str, ContentType] | tuple[str, str, ContentType, str]) -> tuple[SectionSpec, ...]:
    return tuple(SectionSpec(*it) for it in items)


# ---------------------------------------------------------------------------------------------
# Document types (§27 table, every section named there)

DOC_TYPES: dict[str, DocTypeSpec] = {}


def _dt(spec: DocTypeSpec) -> DocTypeSpec:
    DOC_TYPES[spec.doc_type] = spec
    return spec


_dt(DocTypeSpec("PRODUCT_HOME", "Product Home", _secs(
    ("identity", "Identity", S),
    ("ownership", "Product Owner / Engineering Lead / Tech Lead", S),
    ("repository", "Repository / Issue Tracker / Service", S),
    ("classification", "Criticality / Data Classification", S),
    ("stack", "Stack", A),
    ("quick_links", "Quick Links", D),
    ("current_development", "Current Development", D),
    ("active_work", "Active Epics / Features / Release", D),
    ("open_risks", "Open Risks / Security Findings", D),
    ("sdlc_lifecycle", "SDLC Lifecycle", S),
    ("governance_links", "Governance Links", H),
), timing=Timing.PROJECT_CREATION, description="Satu dokumen dengan sections, bukan subfolder."))

_dt(DocTypeSpec("PRODUCT_OVERVIEW", "Product Overview", _secs(
    ("overview", "Overview", A),
    ("vision_objectives", "Vision / Objectives", A),
    ("scope", "Scope", A),
    ("team_ownership", "Team / Ownership", S),
    ("stakeholders", "Stakeholders", A),
    ("roadmap", "Roadmap", A),
    ("system_inventory", "System / Service Inventory", A),
    ("data_overview", "Data Overview", A),
), timing=Timing.PROJECT_CREATION, description="Satu dokumen dengan sections, bukan subfolder."))

_dt(DocTypeSpec("PRD", "PRD", _secs(
    ("overview", "Overview", A),
    ("problem", "Problem", A),
    ("background", "Background", A),
    ("goals_non_goals", "Goals / Non-goals", A),
    ("business_requirements", "Business Requirements", A),
    ("functional_requirements", "Functional Requirements", A),
    ("non_functional_requirements", "Non-functional Requirements", A),
    ("user_stories", "User Stories", A),
    ("acceptance_criteria", "Acceptance Criteria", A),
    ("user_customer_impact", "User / Customer Impact", A),
    ("dependencies", "Dependencies", A),
    ("assumptions", "Assumptions", A),
    ("risks", "Risks", A),
    ("success_metrics", "Success Metrics", A),
    ("rollout_considerations", "Rollout Considerations", A),
    ("related_work", "Related Work Items / RFC / TDD", D),
), template_key="prd", timing=Timing.FEATURE_APPROVAL, description="Satu PRD per fitur yang diperlukan."))

_dt(DocTypeSpec("ARCHITECTURE_OVERVIEW", "Architecture Overview", _secs(
    ("context", "Context", A),
    ("high_level_architecture", "High-level Architecture", A),
    ("component_architecture", "Component Architecture", A),
    ("data_architecture", "Data Architecture", A),
    ("data_flow", "Data Flow", A),
    ("external_integrations", "External Integrations", A),
    ("api_landscape", "API Landscape", A),
    ("infrastructure", "Infrastructure", A),
    ("security_architecture", "Security Architecture", A),
    ("principles", "Principles", A),
    ("decisions", "Decisions", H),
), timing=Timing.PROJECT_CREATION, owner_role="Tech Lead"))

_dt(DocTypeSpec("RFC", "RFC", _secs(
    ("context_problem", "Context / Problem", A),
    ("proposal", "Proposal", A),
    ("goals_non_goals", "Goals / Non-goals", A),
    ("alternatives", "Alternatives", A),
    ("architecture_impact", "Architecture Impact", A),
    ("security_impact", "Security Impact", A),
    ("privacy_impact", "Privacy Impact", A),
    ("operations_impact", "Operations Impact", A),
    ("performance_impact", "Performance Impact", A),
    ("risks_dependencies", "Risks / Dependencies", A),
    ("migration_rollback", "Migration / Rollback", A),
    ("decision_approval", "Decision / Approval", H),
), template_key="rfc", timing=Timing.FEATURE_APPROVAL, owner_role="Tech Lead"))

_dt(DocTypeSpec("TDD", "TDD", _secs(
    ("components", "Components", A),
    ("data_model", "Data Model", A),
    ("data_flow", "Data Flow", A),
    ("api_integration", "API / Integration", A),
    ("authn_authz", "Authn / Authz", A),
    ("controls", "Controls", A),
    ("errors", "Errors", A),
    ("logs_monitoring", "Logs / Monitoring", A),
    ("performance", "Performance", A),
    ("deployment", "Deployment", A),
    ("migration_rollback", "Migration / Rollback", A),
    ("dependencies", "Dependencies", A),
    ("technical_risks", "Technical Risks", A),
    ("testing", "Testing", A),
), template_key="tdd", timing=Timing.FEATURE_APPROVAL, owner_role="Tech Lead"))

_dt(DocTypeSpec("SECURITY_OVERVIEW", "Security Overview", _secs(
    ("ownership", "Ownership", S),
    ("authn_authz", "Authn / Authz", A),
    ("encryption", "Encryption", A),
    ("secrets", "Secrets", A),
    ("logs_monitoring", "Logs / Monitoring", A),
    ("controls", "Controls", A),
), timing=Timing.PROJECT_CREATION, owner_role="Security Lead"))

_dt(DocTypeSpec("RISK_ASSESSMENT", "Risk Assessment", _secs(
    ("context_scope", "Context / Scope", A),
    ("risk_register", "Risk Register", A),
    ("likelihood_impact", "Likelihood / Impact", A),
    ("mitigations", "Mitigations", A),
    ("residual_risk", "Residual Risk", H),
    ("risk_owner", "Risk Owner", S),
    ("acceptance", "Risk Acceptance", H),
), template_key="risk-assessment", timing=Timing.PROJECT_CREATION, owner_role="Product Owner"))

_dt(DocTypeSpec("SECURITY_ASSESSMENT", "Security Assessment", _secs(
    ("scope", "Scope", A),
    ("requirements", "Requirements", A),
    ("risks", "Risks", A),
    ("findings", "Findings", D),
    ("mitigations", "Mitigations", A),
    ("residual_risk", "Residual Risk", H),
    ("approval", "Approval", H),
), template_key="security-assessment", timing=Timing.FEATURE_APPROVAL, owner_role="Security Lead",
    description="Assessment per change."))

_dt(DocTypeSpec("THREAT_MODEL", "Threat Model", _secs(
    ("assets", "Assets", A),
    ("actors", "Actors", A),
    ("trust_boundaries", "Trust Boundaries", A),
    ("entry_points", "Entry Points", A),
    ("threats", "Threats", A),
    ("attack_scenarios", "Attack Scenarios", A),
    ("mitigations", "Mitigations", A),
), template_key="threat-model", timing=Timing.FEATURE_APPROVAL, owner_role="Security Lead"))

_dt(DocTypeSpec("VULNERABILITY_ASSESSMENT", "Vulnerability Assessment", _secs(
    ("scope", "Scope", A),
    ("method_tools", "Method / Tools", A),
    ("findings", "Findings", D, "Hanya dari hasil VA nyata (evidence); tidak pernah dikarang."),
    ("remediation", "Remediation", A),
    ("evidence", "Evidence", D),
), timing=Timing.RELEASE_CANDIDATE, owner_role="Security Lead"))

_dt(DocTypeSpec("PENETRATION_TEST", "Penetration Test", _secs(
    ("scope", "Scope", A),
    ("rules_of_engagement", "Rules of Engagement", H),
    ("findings", "Findings", D, "Hanya dari hasil PT nyata (evidence); tidak pernah dikarang."),
    ("remediation", "Remediation", A),
    ("retest", "Retest", D),
    ("evidence", "Evidence", D),
), timing=Timing.RELEASE_CANDIDATE, owner_role="Security Lead"))

_dt(DocTypeSpec("PRIVACY_ASSESSMENT", "Privacy Assessment / DPIA", _secs(
    ("processing_description", "Processing Description", A),
    ("data_types", "Data Types", A),
    ("necessity_proportionality", "Necessity / Proportionality", A),
    ("risks_to_data_subjects", "Risks to Data Subjects", A),
    ("mitigations", "Mitigations", A),
    ("residual_risk", "Residual Risk", H),
    ("approval", "Approval", H),
), template_key="privacy-assessment-dpia", timing=Timing.FEATURE_APPROVAL, owner_role="Privacy Officer"))

_dt(DocTypeSpec("DATA_INVENTORY", "Data Inventory", _secs(
    ("data_types", "Data Types", A),
    ("classification", "Classification", S),
    ("owner", "Owner", S),
    ("storage", "Storage", A),
    ("access", "Access", A),
    ("retention", "Retention", A),
    ("disposal", "Disposal", A),
), timing=Timing.PROJECT_CREATION, owner_role="Privacy Officer"))

_dt(DocTypeSpec("SECURITY_DECISIONS", "Security & Privacy Decisions", _secs(
    ("decision_log", "Decision Log", H),
    ("exceptions", "Exceptions / Risk Acceptance", H),
), timing=Timing.PROJECT_CREATION, owner_role="Security Lead"))

_dt(DocTypeSpec("DEVELOPMENT_GUIDELINES", "Development Guidelines", _secs(
    ("development_guidelines", "Development Guidelines", A),
    ("coding_standards", "Coding Standards", A),
    ("secure_coding", "Secure Coding", A),
    ("branching", "Branching", A),
    ("pr_review", "PR / Review", A),
    ("dependency_management", "Dependency / Open-source Management", A),
    ("dev_environment", "Dev Environment", A),
    ("secrets", "Secrets", A),
    ("ci_cd_requirements", "CI/CD Requirements", A),
), timing=Timing.PROJECT_CREATION, owner_role="Engineering Lead", description="Development baseline."))

_dt(DocTypeSpec("TEST_PLAN", "Test Plan", _secs(
    ("overview", "Overview", A),
    ("objectives", "Objectives", A),
    ("scope", "Scope", A),
    ("test_strategy", "Test Strategy", A, "Test Strategy tetap section di dalam Test Plan (U-08)."),
    ("approach", "Approach", A),
    ("test_levels", "Test Levels", A),
    ("test_types", "Test Types", A),
    ("environment", "Environment", A),
    ("test_data", "Test Data", A),
    ("tools", "Tools", A),
    ("scenarios", "Scenarios", A),
    ("test_cases", "Test Cases", A),
    ("functional_testing", "Functional Testing", A),
    ("regression_testing", "Regression Testing", A),
    ("performance_testing", "Performance Testing", A),
    ("security_testing", "Security Testing", A),
    ("uat", "UAT", D),
    ("execution_results", "Execution / Results", D),
    ("defect_management", "Defect Management", D),
    ("exit_criteria", "Exit Criteria", A),
    ("final_assessment", "Final Assessment", H),
    ("sign_off", "Sign-off", H),
    ("evidence_references", "Evidence / References", D),
), template_key="test-plan", timing=Timing.FEATURE_APPROVAL, owner_role="QA Lead",
    description="Test Plan per fitur/release; Test Strategy adalah section."))

_dt(DocTypeSpec("RELEASE_PLAN", "Release Plan", _secs(
    ("overview", "Overview", A),
    ("objectives", "Objectives", A),
    ("scope", "Scope", A),
    ("changes", "Features / Enhancements / Fixes / Breaking Changes", A),
    ("related_requirements_architecture", "Related Requirements / Architecture", D),
    ("testing_status", "Testing Status", D),
    ("security_status", "Security Status", D),
    ("uat_status", "UAT Status", D),
    ("known_issues_risks", "Known Issues / Risks", D),
    ("release_notes", "Release Notes", A, "Release Notes tetap section di dalam Release Plan (U-08)."),
    ("deployment", "Deployment", A),
    ("migration", "Migration", A),
    ("rollback", "Rollback", A),
    ("production_readiness", "Production Readiness", D),
    ("sign_off", "Sign-off", H),
    ("post_release_validation", "Post-release Validation", D),
    ("evidence", "Evidence", D),
), template_key="release-plan", timing=Timing.RELEASE_CANDIDATE, owner_role="Release Manager",
    description="Release Plan per versi; Release Notes adalah section."))

_dt(DocTypeSpec("OPERATIONAL_OVERVIEW", "Operational Overview", _secs(
    ("production_architecture", "Production Architecture", A),
    ("ownership", "Ownership", S),
    ("dependencies", "Dependencies", A),
    ("support", "Support", A),
    ("criticality", "Criticality", S),
    ("sli_slo_sla", "SLI / SLO / SLA", A),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("RUNBOOK", "Runbook", _secs(
    ("purpose_trigger", "Purpose / Trigger", A),
    ("prerequisites", "Prerequisites", A),
    ("procedure", "Procedure", A),
    ("verification", "Verification", A),
    ("rollback", "Rollback", A),
    ("escalation", "Escalation", A),
), template_key="runbook", timing=Timing.RELEASE_CANDIDATE, owner_role="Operations Owner",
    description="Runbook restart, DB recovery, certificate renewal, failover yang relevan."))

_dt(DocTypeSpec("MONITORING", "Monitoring", _secs(
    ("metrics", "Metrics", A),
    ("dashboards", "Dashboards", D),
    ("coverage", "Coverage", D),
    ("thresholds", "Thresholds", A),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("LOGGING", "Logs", _secs(
    ("application_logs", "Application Logs", A),
    ("audit_logs", "Audit Logs", A),
    ("security_logs", "Security Logs", A),
    ("retention", "Retention", A),
    ("access", "Access", A),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("ALERTING", "Alerts", _secs(
    ("alert_rules", "Alert Rules", A),
    ("severity", "Severity", A),
    ("notification", "Notification", A),
    ("escalation", "Escalation", A),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("SLO", "Service Level Objectives", _secs(
    ("availability", "Availability Objective", A),
    ("latency", "Latency Objective", A),
    ("error_rate", "Error Objective", A),
    ("error_budget", "Error Budget / Measurement", D),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("BACKUP_RESTORE", "Backup & Restore", _secs(
    ("scope", "Backup Scope", A),
    ("frequency", "Frequency", A),
    ("retention", "Retention", A),
    ("storage", "Storage", A),
    ("restore_procedure", "Restore Procedure", A),
    ("restore_testing", "Restore Testing", D, "Hanya dari restore test nyata; tanpa bukti = belum teruji."),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("MAINTENANCE", "Maintenance", _secs(
    ("jobs", "Jobs", A),
    ("checks", "Checks", A),
    ("routine_activities", "Routine Activities", A),
), timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"))

_dt(DocTypeSpec("INCIDENT_REPORT", "Incident Report", _secs(
    ("summary", "Incident Summary", A),
    ("impact", "Impact", A),
    ("detection", "Detection", D),
    ("timeline", "Timeline", D),
    ("response", "Response", A),
    ("root_cause", "Root Cause", A),
    ("resolution", "Resolution", A),
    ("corrective_actions", "Corrective Actions", A),
    ("preventive_actions", "Preventive Actions", A),
    ("evidence", "Evidence", D),
), timing=Timing.INCIDENT, owner_role="Incident Commander"))

_dt(DocTypeSpec("PROBLEM_RECORD", "Problem Record", _secs(
    ("problem_statement", "Problem Statement", A),
    ("root_cause", "Root Cause", A),
    ("impact", "Impact", A),
    ("actions", "Actions", A),
    ("validation", "Validation", D),
), timing=Timing.INCIDENT, owner_role="Problem Manager"))

_dt(DocTypeSpec("POST_MORTEM", "Post-Mortem", _secs(
    ("summary", "Incident Summary", A),
    ("impact", "Impact", A),
    ("detection", "Detection", D),
    ("timeline", "Timeline", D),
    ("response", "Response", A),
    ("root_cause", "Root Cause", A),
    ("resolution", "Resolution", A),
    ("corrective_actions", "Corrective Actions", A),
    ("preventive_actions", "Preventive Actions", A),
    ("lessons_learned", "Lessons Learned", A),
    ("evidence", "Evidence", D),
), template_key="post-mortem", timing=Timing.INCIDENT, owner_role="Incident Commander"))

_dt(DocTypeSpec("CHANGE_RECORD", "Change Record", _secs(
    ("description", "Description", A),
    ("reason", "Reason", A),
    ("scope", "Scope", A),
    ("risk", "Risk", A),
    ("impact", "Impact", A),
    ("implementation", "Implementation", D),
    ("testing", "Testing", D),
    ("approval", "Approval", H),
    ("rollback", "Rollback", A),
    ("validation", "Validation", D),
    ("evidence", "Evidence", D),
), timing=Timing.IMPLEMENTATION_PR, owner_role="Change Owner"))

_dt(DocTypeSpec("IMPROVEMENT_RECORD", "Improvement", _secs(
    ("opportunity", "Opportunity", A),
    ("current_state", "Current State", A),
    ("proposal", "Proposal", A),
    ("expected_benefit", "Expected Benefit", A),
    ("risk", "Risk", A),
    ("owner", "Owner", S),
    ("implementation", "Implementation", D),
    ("result", "Result", D),
    ("metrics", "Metrics", D),
), timing=Timing.IMPLEMENTATION_PR, owner_role="Product Owner"))

_dt(DocTypeSpec("RETIREMENT_PLAN", "Retirement & Decommission Plan", _secs(
    ("overview", "Overview", A),
    ("scope", "Scope", A),
    ("impact", "Impact", A),
    ("data_closure", "Data Closure", A),
    ("access_closure", "Access Closure", A),
    ("integration_closure", "Integration Closure", A),
    ("infrastructure_closure", "Infrastructure Closure", A),
    ("security_closure", "Security Closure", A),
    ("privacy_closure", "Privacy Closure", A),
    ("backup_closure", "Backup Closure", A),
    ("communication", "Communication", A),
    ("execution", "Execution", D),
    ("validation", "Validation", D),
    ("evidence", "Evidence", D),
    ("final_approval", "Final Approval", H),
), template_key="retirement-decommission", timing=Timing.RETIREMENT, owner_role="Product Owner"))


# ---------------------------------------------------------------------------------------------
# Templates folder (§27): exactly these twelve.

@dataclass(frozen=True)
class TemplateSpec:
    key: str
    title: str
    doc_type: str


TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec("prd", "PRD", "PRD"),
    TemplateSpec("rfc", "RFC", "RFC"),
    TemplateSpec("tdd", "TDD", "TDD"),
    TemplateSpec("risk-assessment", "Risk Assessment", "RISK_ASSESSMENT"),
    TemplateSpec("security-assessment", "Security Assessment", "SECURITY_ASSESSMENT"),
    TemplateSpec("threat-model", "Threat Model", "THREAT_MODEL"),
    TemplateSpec("privacy-assessment-dpia", "Privacy Assessment / DPIA", "PRIVACY_ASSESSMENT"),
    TemplateSpec("test-plan", "Test Plan", "TEST_PLAN"),
    TemplateSpec("release-plan", "Release Plan", "RELEASE_PLAN"),
    TemplateSpec("runbook", "Runbook", "RUNBOOK"),
    TemplateSpec("post-mortem", "Post-Mortem", "POST_MORTEM"),
    TemplateSpec("retirement-decommission", "Retirement & Decommission", "RETIREMENT_PLAN"),
)
TEMPLATE_BY_KEY = {t.key: t for t in TEMPLATES}

# Runbooks the master names (§27 08. Operations) — created per relevance, never pre-filled.
SUGGESTED_RUNBOOKS: tuple[tuple[str, str], ...] = (
    ("restart", "Restart"),
    ("db-recovery", "DB Recovery"),
    ("certificate-renewal", "Certificate Renewal"),
    ("failover", "Failover"),
)


# ---------------------------------------------------------------------------------------------
# The tree

F = NodeKind.FOLDER
DOC = NodeKind.DOCUMENT

TREE: tuple[Node, ...] = (
    Node("00-product-home", "00", "00. Product Home", DOC, doc_type="PRODUCT_HOME",
         description="Identity, ownership, repository/tracker/service, criticality, stack, quick links, current "
                     "development, active work, open risks, SDLC lifecycle, governance links."),
    Node("01-product-overview", "01", "01. Product Overview", DOC, doc_type="PRODUCT_OVERVIEW"),
    Node("02-requirements", "02", "02. Requirements", F, collection="PRD", collection_per="feature",
         timing=Timing.FEATURE_APPROVAL, description="Folder PRDs, satu PRD per fitur yang diperlukan."),
    Node("03-architecture-design", "03", "03. Architecture & Design", F, owner_role="Tech Lead", children=(
        Node("03-architecture-overview", "03", "Architecture Overview", DOC, doc_type="ARCHITECTURE_OVERVIEW",
             owner_role="Tech Lead"),
        Node("03-rfcs", "03", "RFCs", F, collection="RFC", collection_per="change", timing=Timing.FEATURE_APPROVAL,
             owner_role="Tech Lead", description="RFC bila diperlukan oleh perubahan."),
        Node("03-tdds", "03", "TDDs", F, collection="TDD", collection_per="component", timing=Timing.FEATURE_APPROVAL,
             owner_role="Tech Lead", description="TDD bila diperlukan oleh komponen/fitur."),
    )),
    Node("04-security-privacy", "04", "04. Security & Privacy", F, owner_role="Security Lead", children=(
        Node("04-security-overview", "04", "Security Overview", DOC, doc_type="SECURITY_OVERVIEW",
             owner_role="Security Lead"),
        Node("04-security-assessments", "04", "Security Assessments", F, collection="SECURITY_ASSESSMENT",
             collection_per="change", owner_role="Security Lead",
             description="Assessment per change; initial risk assessment saat project creation."),
        Node("04-threat-models", "04", "Threat Models", F, collection="THREAT_MODEL", collection_per="scope",
             timing=Timing.FEATURE_APPROVAL, owner_role="Security Lead"),
        Node("04-vulnerability-assessments", "04", "Vulnerability Assessments", F,
             collection="VULNERABILITY_ASSESSMENT", collection_per="assessment", timing=Timing.RELEASE_CANDIDATE,
             owner_role="Security Lead", description="Hanya hasil VA nyata."),
        Node("04-penetration-tests", "04", "Penetration Tests", F, collection="PENETRATION_TEST",
             collection_per="test", timing=Timing.RELEASE_CANDIDATE, owner_role="Security Lead",
             description="Hanya hasil PT nyata."),
        Node("04-privacy-assessments", "04", "Privacy Assessments", F, collection="PRIVACY_ASSESSMENT",
             collection_per="change", timing=Timing.FEATURE_APPROVAL, owner_role="Privacy Officer"),
        Node("04-data-inventory", "04", "Data Inventory", DOC, doc_type="DATA_INVENTORY", owner_role="Privacy Officer"),
        Node("04-security-privacy-decisions", "04", "Security & Privacy Decisions", DOC, doc_type="SECURITY_DECISIONS",
             owner_role="Security Lead"),
    )),
    Node("05-development", "05", "05. Development", DOC, doc_type="DEVELOPMENT_GUIDELINES",
         owner_role="Engineering Lead", description="Development baseline (satu dokumen dengan sections)."),
    Node("06-testing", "06", "06. Testing", F, collection="TEST_PLAN", collection_per="feature",
         timing=Timing.FEATURE_APPROVAL, owner_role="QA Lead",
         description="Test Plans per fitur/release; Test Strategy section di dalam Test Plan."),
    Node("07-release-deployment", "07", "07. Release & Deployment", F, collection="RELEASE_PLAN",
         collection_per="release", timing=Timing.RELEASE_CANDIDATE, owner_role="Release Manager",
         description="Release Plans per versi; Release Notes section di dalam Release Plan."),
    Node("08-operations", "08", "08. Operations", F, timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner",
         children=(
             Node("08-operational-overview", "08", "Operational Overview", DOC, doc_type="OPERATIONAL_OVERVIEW",
                  timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"),
             Node("08-runbooks", "08", "Runbooks", F, collection="RUNBOOK", collection_per="runbook",
                  timing=Timing.RELEASE_CANDIDATE, owner_role="Operations Owner"),
             Node("08-monitoring", "08", "Monitoring", DOC, doc_type="MONITORING", timing=Timing.PRODUCTION_VALIDATION,
                  owner_role="Operations Owner"),
             Node("08-logs", "08", "Logs", DOC, doc_type="LOGGING", timing=Timing.PRODUCTION_VALIDATION,
                  owner_role="Operations Owner"),
             Node("08-alerts", "08", "Alerts", DOC, doc_type="ALERTING", timing=Timing.PRODUCTION_VALIDATION,
                  owner_role="Operations Owner"),
             Node("08-slo", "08", "Service Level Objectives", DOC, doc_type="SLO", timing=Timing.PRODUCTION_VALIDATION,
                  owner_role="Operations Owner"),
             Node("08-backup-restore", "08", "Backup & Restore", DOC, doc_type="BACKUP_RESTORE",
                  timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"),
             Node("08-maintenance", "08", "Maintenance", DOC, doc_type="MAINTENANCE",
                  timing=Timing.PRODUCTION_VALIDATION, owner_role="Operations Owner"),
         )),
    Node("09-incidents-problems", "09", "09. Incidents & Problems", F, timing=Timing.INCIDENT,
         owner_role="Incident Commander", children=(
             Node("09-incidents", "09", "Incidents", F, collection="INCIDENT_REPORT", collection_per="incident",
                  timing=Timing.INCIDENT, owner_role="Incident Commander"),
             Node("09-problems", "09", "Problems", F, collection="PROBLEM_RECORD", collection_per="problem",
                  timing=Timing.INCIDENT, owner_role="Problem Manager"),
         )),
    Node("10-changes", "10", "10. Changes", F, collection="CHANGE_RECORD", collection_per="change",
         timing=Timing.IMPLEMENTATION_PR, owner_role="Change Owner"),
    Node("11-improvements", "11", "11. Improvements", F, collection="IMPROVEMENT_RECORD",
         collection_per="improvement", timing=Timing.IMPLEMENTATION_PR, owner_role="Product Owner"),
    Node("12-retirement", "12", "12. Retirement", DOC, doc_type="RETIREMENT_PLAN", timing=Timing.RETIREMENT,
         owner_role="Product Owner", description="Retirement & Decommission Plan."),
    Node("templates", "TPL", "Templates", F, collection="TEMPLATE", collection_per="template",
         timing=Timing.STATIC_TEMPLATE, description="12 template generik tanpa data tenant."),
)


# §41 "Kriteria kesiapan menentukan apakah bagian dokumentasi wajib lengkap sebelum tahap berikutnya":
# sections that must not be "documentation pending" when a stage is passed. The release candidate snapshot is
# refused while its required sections are pending; earlier stages are reported in the publication preview.
REQUIRED_SECTIONS: dict[Timing, dict[str, tuple[str, ...]]] = {
    Timing.PROJECT_CREATION: {
        "PRODUCT_OVERVIEW": ("overview", "scope"),
        "ARCHITECTURE_OVERVIEW": ("context",),
        "SECURITY_OVERVIEW": ("authn_authz",),
        "RISK_ASSESSMENT": ("risk_register",),
    },
    Timing.FEATURE_APPROVAL: {
        "PRD": ("overview", "functional_requirements", "acceptance_criteria"),
        "TEST_PLAN": ("scope", "test_cases"),
    },
    Timing.RELEASE_CANDIDATE: {
        "RELEASE_PLAN": ("scope", "changes", "release_notes", "deployment", "migration", "rollback"),
    },
}


def required_pending(documents: Any, stage: Timing) -> list[dict[str, Any]]:
    """Required sections of *stage* that are still documentation pending, per rendered document."""
    wanted = REQUIRED_SECTIONS.get(stage, {})
    out: list[dict[str, Any]] = []

    def field(s: Any, name: str) -> Any:
        return s.get(name) if isinstance(s, dict) else getattr(s, name, None)

    for d in documents:  # RenderedDocument or DocumentRecord (persisted sections are dicts)
        keys = wanted.get(getattr(d, "doc_type", ""))
        if not keys or getattr(d, "status", "") == "SUPERSEDED":
            continue
        missing = [field(s, "title") for s in (getattr(d, "sections", None) or [])
                   if field(s, "key") in keys and field(s, "status") == "PENDING"]
        if missing:
            out.append({"logical_doc_id": d.logical_doc_id, "title": d.title, "sections": missing})
    return out


def product_title(name: str) -> str:
    return PRODUCT_TITLE_TEMPLATE.format(name=name)


def iter_nodes(nodes: tuple[Node, ...] = TREE, parent: Node | None = None):
    """Depth-first (node, parent) pairs."""
    for n in nodes:
        yield n, parent
        yield from iter_nodes(n.children, n)


def node_by_key(key: str) -> Node:
    for n, _p in iter_nodes():
        if n.key == key:
            return n
    raise KeyError(key)


def structure() -> dict[str, Any]:
    """JSON description used by ``GET /api/docs/product1-structure`` and the UI."""
    return {
        "structure_version": STRUCTURE_VERSION,
        "title_template": PRODUCT_TITLE_TEMPLATE.format(name="<Nama Produk>"),
        "root": {"kind": NodeKind.FOLDER.value, "title": "<nama proyek>",
                 "note": "Folder luar memakai nama proyek (§27)."},
        "nodes": [n.to_dict() for n in TREE],
        "doc_types": {k: v.to_dict() for k, v in DOC_TYPES.items()},
        "templates": [{"key": t.key, "title": t.title, "doc_type": t.doc_type} for t in TEMPLATES],
        "content_types": CONTENT_TYPE_DESCRIPTIONS,
        "timing": [dict(r) for r in TIMING_TABLE],
        "rules": [
            "Folder/parent, dokumen/page dan section dibedakan; daftar isi tidak menjadi subpage.",
            "Product Home dan Product Overview adalah satu dokumen dengan sections.",
            "Test Strategy adalah section dalam Test Plan; Release Notes adalah section dalam Release Plan.",
            "Tidak membuat hasil PT/VA, restore test, UAT atau approval palsu; node masa depan NOT STARTED/"
            "NOT APPLICABLE dengan alasan dan owner.",
        ],
    }


# ---------------------------------------------------------------------------------------------
# Templates on disk (blueprints/product1/templates/<key>.md); generated from the spec as fallback

def templates_dir() -> Path:
    return repo_root() / "blueprints" / "product1" / "templates"


def template_from_spec(key: str) -> str:
    """Generic template markdown generated from the doc-type spec (no tenant data)."""
    t = TEMPLATE_BY_KEY[key]
    spec = DOC_TYPES[t.doc_type]
    lines = [
        f"# {t.title} Template",
        "",
        "<!-- aeos:template=" + key + " structure_version=" + STRUCTURE_VERSION + " -->",
        "",
        "> Template generik PRODUCT 1. Tanpa data tenant, credential atau konten kantor.",
        "> Tipe konten per section: STATIC, DYNAMIC (rujuk sumber live), AI_MAINTAINED (narasi usulan),",
        "> HUMAN_GOVERNED (diisi manusia yang berwenang).",
        "",
        "| Metadata | Nilai |",
        "|---|---|",
        "| Logical doc ID | |",
        "| Project / domain | |",
        "| Owner | |",
        "| Status | draft / approved / published / superseded |",
        "| Source plan / version | |",
        "| External revision | |",
        "| Last reviewed / last synced | |",
        "| Links (requirement / commit / issue / evidence) | |",
        "",
    ]
    for s in spec.sections:
        lines += [f"## {s.title}", "", f"_Content type: {s.content_type.value}_" + (f" — {s.note}" if s.note else ""), ""]
        if s.content_type == ContentType.HUMAN_GOVERNED:
            lines += ["_Diisi oleh manusia yang berwenang. Tidak diisi oleh AEOS atau model._", ""]
        elif s.content_type == ContentType.DYNAMIC:
            lines += ["_Rujuk sumber live (issue tracker / verifier / CI / evidence). Jangan menyalin status._", ""]
        else:
            lines += ["<!-- isi bagian ini; tandai 'documentation pending' bila belum diketahui -->", ""]
    return "\n".join(lines).rstrip() + "\n"


@lru_cache(maxsize=32)
def load_template(key: str) -> str:
    if key not in TEMPLATE_BY_KEY:
        raise KeyError(key)
    path = templates_dir() / f"{key}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return template_from_spec(key)
