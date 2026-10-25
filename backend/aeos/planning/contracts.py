"""JSON Schemas for planning stage outputs (§14, §15, §21, §27, §34).

The schemas are built here (single source) and published to ``contracts/planning/*.schema.json``
(``aeos plan schemas --write``; a test keeps the files in sync). They are plain JSON Schema draft
2020-12 objects acceptable to ``claude -p --json-schema``: every object has
``additionalProperties: false`` and lists its properties as required; no ``$ref``, no recursion,
no numeric/string-length constraints (structured-output limits). Bounds that matter (attempt caps,
DAG validity, classification floors) are enforced by code at compile time, not by the schema.

Schema 1, revision 2 (additive): top-level ``feature_governance`` (owner, RFC/TDD/DPIA/security-assessment
needs, threat model per feature), ``milestone_governance``, ``rfcs``, ``tdds`` and ``interface_contracts``
(contract-first, §19/§22). The schema title stays ``plan-content/1`` because nothing was removed or renamed;
content stored before revision 2 is read through ``upgrade_content`` (empty lists = nothing requested).
Revision 3 (additive, nested): ``operations`` gains ``support``, ``log_policy``, ``alert_policy``,
``backup.storage``, ``maintenance_checks`` and ``routine_activities`` (§27 08. sections that had no source),
and the diagram kind ``cicd`` (§29); older content gets them empty (= not given).
Revision 4 (additive, nested): every ``operations.runbooks`` entry gains ``prerequisites``, ``procedure``,
``verification``, ``rollback`` and ``escalation`` (§27 08. Runbooks proposals); older entries get them empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aeos.core.paths import repo_root

SCHEMA_VERSION = "1"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
RISK_CLASSES = ["R0", "R1", "R2", "R3", "R4"]
DATA_CLASSES = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
PRIORITIES = ["P0", "P1", "P2", "P3"]
LEVELS = ["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
DIAGRAM_KINDS = ["context", "component", "sequence", "erd", "data_flow", "trust_boundary", "deployment", "cicd",
                 "journey"]
SCENARIO_TYPES = ["functional", "regression", "integration", "negative", "edge", "performance", "security",
                  "accessibility", "uat"]
QUESTION_CATEGORIES = ["architecture", "scope", "cost", "security", "legal", "external_target"]
PLAN_VERDICTS = ["PASS-STATIC", "NOT-PROVEN", "BLOCKED", "FAIL"]  # a plan review runs no tests


def _s(description: str = "") -> dict[str, Any]:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _i(description: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": description} if description else {"type": "integer"}


def _b(description: str = "") -> dict[str, Any]:
    return {"type": "boolean", "description": description} if description else {"type": "boolean"}


def _e(values: list[str], description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string", "enum": list(values)}
    if description:
        out["description"] = description
    return out


def _a(items: dict[str, Any], description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": "array", "items": items}
    if description:
        out["description"] = description
    return out


def _sa(description: str = "") -> dict[str, Any]:
    return _a(_s(), description)


def _o(properties: dict[str, Any], description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": properties, "required": list(properties),
                           "additionalProperties": False}
    if description:
        out["description"] = description
    return out


# ---------------------------------------------------------------- plan content (PlanVersion.content)

PRODUCT_HOME = _o({
    "name": _s("Product name (display only)"),
    "summary": _s(),
    "product_owner": _s("Named only if given in the input; otherwise UNKNOWN (never invent stakeholders)"),
    "engineering_lead": _s("UNKNOWN unless provided"),
    "tech_lead": _s("UNKNOWN unless provided"),
    "repository": _s("Repository reference from the context, or UNKNOWN"),
    "issue_tracker": _s("GitHub Issues (PERSONAL) / Jira (OFFICE) target, or UNKNOWN"),
    "service": _s(),
    "criticality": _e(LEVELS),
    "data_classification": _e(DATA_CLASSES),
    "stack": _sa(),
    "quick_links": _sa(),
    "current_development": _s(),
    "active_features": _sa("Feature keys"),
    "open_risks": _sa("Risk keys from security_privacy.initial_risks"),
    "sdlc_lifecycle": _s(),
    "governance_links": _sa(),
}, "00. Product Home")

OVERVIEW = _o({
    "overview": _s(),
    "vision": _s(),
    "objectives": _sa(),
    "scope_in": _sa(),
    "scope_out": _sa(),
    "team_ownership": _s(),
    "stakeholders": _sa("Only stakeholders named in the input; mark assumptions"),
    "roadmap": _sa("Milestone-ordered roadmap lines"),
    "system_inventory": _sa(),
    "data_overview": _s(),
}, "01. Product Overview")

PRD = _o({
    "feature_key": _s(),
    "title": _s(),
    "overview": _s(),
    "problem": _s(),
    "background": _s(),
    "goals": _sa(),
    "non_goals": _sa(),
    "business_requirements": _sa(),
    "functional_requirements": _sa(),
    "non_functional_requirements": _sa(),
    "user_stories": _sa(),
    "acceptance_criteria": _sa(),
    "user_impact": _s(),
    "dependencies": _sa(),
    "assumptions": _sa(),
    "risks": _sa(),
    "success_metrics": _sa(),
    "rollout_considerations": _s(),
    "related_work": _sa("Related work items / RFC / TDD references"),
}, "02. Requirements: one PRD per feature")

DIAGRAM = _o({
    "key": _s(),
    "title": _s(),
    "kind": _e(DIAGRAM_KINDS),
    "mermaid": _s("Mermaid source; never runtime evidence"),
    "verification_source": _s("Checked against (contract/manifest/schema/threat model; test node ids)"),
})

ARCHITECTURE = _o({
    "overview": _o({
        "context": _s(),
        "high_level_architecture": _s(),
        "component_architecture": _sa(),
        "data_architecture": _s(),
        "data_flow": _s(),
        "external_integrations": _sa(),
        "api_landscape": _sa(),
        "infrastructure": _s(),
        "security_architecture": _s(),
        "principles": _sa(),
        "decisions": _sa("Proposed decisions (ADR/Decision only after human approval)"),
    }),
    "diagrams": _a(DIAGRAM),
}, "03. Architecture & Design")

RISK = _o({
    "key": _s(),
    "title": _s(),
    "description": _s(),
    "likelihood": _e(LEVELS),
    "impact": _e(LEVELS),
    "mitigation": _s(),
    "residual_risk": _s("Residual risk is accepted only by a human RISK_ACCEPTANCE decision"),
    "owner": _s(),
})
THREAT = _o({"key": _s(), "threat": _s(), "scenario": _s(), "mitigation": _s()})
DATA_ITEM = _o({"data_type": _s(), "classification": _e(DATA_CLASSES), "owner": _s(), "storage": _s(),
                "access": _s(), "retention": _s(), "disposal": _s()})

SECURITY = _o({
    "overview": _o({"ownership": _s(), "authn_authz": _s(), "encryption": _s(), "secrets": _s(),
                    "logging_monitoring": _s(), "controls": _sa()}),
    "initial_risks": _a(RISK),
    "threat_model": _o({"assets": _sa(), "actors": _sa(), "trust_boundaries": _sa(), "entry_points": _sa(),
                        "threats": _a(THREAT)}),
    "data_inventory": _a(DATA_ITEM),
    "open_questions": _sa(),
}, "04. Security & Privacy: initial risk/threat notes (no VA/PT results are claimed)")

DEVELOPMENT = _o({
    "guidelines": _sa(),
    "coding_standards": _sa(),
    "secure_coding": _sa(),
    "branching": _s(),
    "pr_review": _s(),
    "dependency_management": _s(),
    "dev_environment": _s(),
    "secrets_handling": _s(),
    "ci_cd_requirements": _sa(),
}, "05. Development baseline")

SCENARIO = _o({"key": _s(), "title": _s(), "type": _e(SCENARIO_TYPES), "steps": _sa(), "expected_result": _s(),
               "acceptance_criteria_refs": _sa()})
TEST_PLAN = _o({
    "key": _s(),
    "title": _s(),
    "feature_keys": _sa(),
    "overview": _s(),
    "objectives": _sa(),
    "scope": _s(),
    "test_strategy": _o({"approach": _s(), "levels": _sa(), "types": _sa(), "environments": _sa(), "data": _s(),
                         "tools": _sa()}, "Test Strategy is a section of the Test Plan (U-08)"),
    "scenarios": _a(SCENARIO),
    "acceptance_criteria": _sa(),
    "exit_criteria": _sa(),
    "defect_management": _s(),
    "evidence_requirements": _sa(),
})
TESTING = _o({"test_plans": _a(TEST_PLAN)}, "06. Testing (no execution results or sign-off are produced by planning)")

RELEASE_PLAN = _o({
    "key": _s(),
    "version": _s(),
    "title": _s(),
    "overview": _s(),
    "scope": _s(),
    "features": _sa(),
    "enhancements": _sa(),
    "fixes": _sa(),
    "breaking_changes": _sa(),
    "related_requirements": _sa(),
    "known_issues": _sa(),
    "risks": _sa(),
    "release_notes": _o({"summary": _s(), "highlights": _sa(), "upgrade_notes": _s(), "known_limitations": _sa()},
                        "Release Notes are a section of the Release Plan (U-08)"),
    "deployment": _s(),
    "migration": _s(),
    "rollback": _s(),
    "production_readiness": _sa(),
    "post_release_validation": _sa(),
})
RELEASE = _o({"release_plans": _a(RELEASE_PLAN)}, "07. Release & Deployment")

OPERATIONS = _o({
    "overview": _s(),
    "ownership": _s(),
    "support": _s(),
    "dependencies": _sa(),
    "criticality": _e(LEVELS),
    "sli_slo_proposals": _a(_o({"indicator": _s(), "objective": _s(), "rationale": _s()})),
    "runbooks": _a(_o({"key": _s(), "title": _s(), "purpose": _s(), "prerequisites": _s(), "procedure": _s(),
                       "verification": _s(), "rollback": _s(), "escalation": _s()})),
    "monitoring": _sa(),
    "logging": _s(),
    "log_policy": _o({"audit_logs": _s(), "security_logs": _s(), "retention": _s(), "access": _s()}),
    "alerting": _sa(),
    "alert_policy": _o({"severity": _s(), "notification": _s(), "escalation": _s()}),
    "backup": _o({"scope": _s(), "frequency": _s(), "retention": _s(), "restore_procedure": _s(), "storage": _s()}),
    "maintenance": _sa(),
    "maintenance_checks": _sa(),
    "routine_activities": _sa(),
}, "08. Operations (proposals; restore/SLO evidence comes from runtime only)")
# Schema 1, revision 3 (additive, nested): operations fields added after content was stored. ``upgrade_content``
# fills them empty (= not given; the document section stays documentation pending, never invented). They carry no
# descriptions: the synthesis schema must still fit the Claude CLI command line WITH its annotations
# (tests/unit/test_planning_bridge_seam.py::test_planning_contract_schemas_fit_the_windows_command_line).
OPERATIONS_ADDITIVE: dict[str, Any] = {
    "support": "", "log_policy": {"audit_logs": "", "security_logs": "", "retention": "", "access": ""},
    "alert_policy": {"severity": "", "notification": "", "escalation": ""}, "maintenance_checks": [],
    "routine_activities": [],
}
BACKUP_ADDITIVE: dict[str, Any] = {"storage": ""}
# Revision 4 (additive, nested): each ``operations.runbooks`` entry gains the §27 08. Runbook procedure fields as
# AI_MAINTAINED proposals (a reviewed runbook of the project's repository still wins). Older entries get them empty
# (= not given; the section stays documentation pending). No descriptions, for the same command-line budget.
RUNBOOK_ADDITIVE: dict[str, Any] = {"prerequisites": "", "procedure": "", "verification": "", "rollback": "",
                                    "escalation": ""}

MILESTONE = _o({"key": _s(), "title": _s(), "exit_evidence": _s(), "order": _i()})
FEATURE = _o({
    "key": _s("Stable feature key, e.g. F1"),
    "title": _s(),
    "requirement": _s(),
    "priority": _e(PRIORITIES),
    "acceptance": _sa(),
    "depends_on": _sa("Feature keys"),
    "milestone": _s("Milestone key"),
})
TASK = _o({
    "key": _s("Stable task key, e.g. T1"),
    "feature_key": _s(),
    "title": _s(),
    "goal": _s(),
    "role_key": _s("RoleSkillPack key, e.g. feature-implementer, backend-api-engineer, qa-test-designer"),
    "input_artifacts": _sa(),
    "expected_outputs": _sa(),
    "acceptance_criteria": _sa(),
    "depends_on": _sa("Task keys within this plan (must form a DAG)"),
    "allowed_paths": _sa("Repository paths/globs this task may write"),
    "forbidden_paths": _sa(),
    "tools_allowed": _sa(),
    "egress_allowed": _sa("Hosts; default deny (empty)"),
    "capability_scope": _o({"operations": _sa(), "paths": _sa(), "notes": _s()}),
    "capability_ttl_seconds": _i(),
    "data_classification": _e(DATA_CLASSES),
    "model_route_policy": _o({"pool": _e(["WORKER", "LOCAL"]), "requires_tools": _b(),
                              "min_context_tokens": _i("0 = no minimum"), "notes": _s()}),
    "reviewer_policy": _o({"required": _b(), "reviewer_role": _s(), "checkpoint": _s()}),
    "verifier_policy": _o({"required": _b(), "suites": _sa(), "negative_tests_required": _b()}),
    "limits": _o({"max_attempts": _i("Pilot cap: at most 2"), "timeout_seconds": _i(), "max_output_tokens": _i()}),
    "stop_conditions": _sa(),
    "evidence_requirements": _sa(),
    "risk_class": _e(RISK_CLASSES),
    "artifact_schema_version": _s(),
}, "TaskSpec draft with every §21 field")

STATEMENT = _o({"statement": _s(), "source": _s()})
UNKNOWN = _o({"statement": _s(), "impact": _s()})
QUESTION = _o({"question": _s(), "category": _e(QUESTION_CATEGORIES), "blocking": _b()})

# ---- feature approval artefacts (§27 "03. RFCs/TDDs", "04. Privacy assessments", §28 feature approval) -------
# Additive top-level sections (schema 1, revision 2): older stored content without them is upgraded with
# empty lists by ``upgrade_content`` — an empty list keeps its meaning (nothing requested / UNKNOWN).
PERSONAL_DATA = ["UNKNOWN", "YES", "NO"]
CONTRACT_KINDS = ["api", "schema", "design_tokens", "shared_interface", "event"]
RFC = _o({
    "key": _s(), "feature_key": _s("Feature key, or empty for a plan-level RFC"), "title": _s(),
    "context_problem": _s(), "proposal": _s(), "goals_non_goals": _sa(), "alternatives": _sa(),
    "architecture_impact": _s(), "security_impact": _s(), "privacy_impact": _s(), "operations_impact": _s(),
    "performance_impact": _s(), "risks_dependencies": _sa(), "migration_rollback": _s(),
}, "RFC proposal (decision/approval stays human)")
TDD = _o({
    "key": _s(), "feature_key": _s("Feature key, or empty"), "title": _s(), "components": _sa(),
    "data_model": _s(), "data_flow": _s(), "api_integration": _s(), "authn_authz": _s(), "controls": _sa(),
    "errors": _s(), "logs_monitoring": _s(), "performance": _s(), "deployment": _s(), "migration_rollback": _s(),
    "dependencies": _sa(), "technical_risks": _sa(), "testing": _s(),
}, "Technical design document")
FEATURE_GOVERNANCE = _o({
    "feature_key": _s(),
    "owner": _s("Feature owner only if named in the input; otherwise UNKNOWN"),
    "needs_rfc": _b("true when an RFC entry with this feature_key is given"),
    "needs_tdd": _b("true when a TDD entry with this feature_key is given"),
    "personal_data": _e(PERSONAL_DATA, "Does the feature process personal data? UNKNOWN when not known"),
    "privacy": _o({"processing_description": _s(), "data_types": _sa(), "necessity_proportionality": _s(),
                   "risks_to_data_subjects": _sa(), "mitigations": _sa()}, "DPIA input when personal_data=YES"),
    "needs_security_assessment": _b(),
    "security_assessment": _o({"scope": _s(), "requirements": _sa(), "risks": _sa(), "mitigations": _sa()}),
    "threat_model": _o({"assets": _sa(), "actors": _sa(), "trust_boundaries": _sa(), "entry_points": _sa(),
                        "threats": _a(THREAT)}, "Feature threat model; empty when none"),
}, "Per-feature approval artefacts; no findings, approvals or residual-risk acceptance")
MILESTONE_GOVERNANCE = _o({
    "milestone_key": _s(), "priority": _e(PRIORITIES), "acceptance": _sa(), "depends_on": _sa("Milestone keys"),
    "owner": _s("UNKNOWN unless provided"),
})
INTERFACE_CONTRACT = _o({
    "key": _s(), "title": _s(), "kind": _e(CONTRACT_KINDS), "feature_keys": _sa(),
    "provider_task_key": _s("Task that defines and stabilises the contract"),
    "consumer_task_keys": _sa("Tasks built against it; each must depend on the provider task"),
    "definition": _s("Endpoints/schemas/tokens in short form"), "compatibility": _s(),
    "contract_paths": _sa("Repository paths of the contract files, e.g. contracts/api.json"),
}, "Contract-first: agreed before parallel work that depends on it")
ADDITIVE_SECTIONS: tuple[str, ...] = ("rfcs", "tdds", "feature_governance", "milestone_governance",
                                      "interface_contracts")

PLAN_CONTENT_PROPERTIES: dict[str, Any] = {
    "schema_version": _e([SCHEMA_VERSION]),
    "product_home": PRODUCT_HOME,
    "overview": OVERVIEW,
    "requirements": _a(PRD),
    "architecture": ARCHITECTURE,
    "security_privacy": SECURITY,
    "development_baseline": DEVELOPMENT,
    "testing": TESTING,
    "release": RELEASE,
    "operations": OPERATIONS,
    "milestones": _a(MILESTONE),
    "features": _a(FEATURE),
    "tasks": _a(TASK),
    "feature_governance": _a(FEATURE_GOVERNANCE, "One entry per feature (owner, RFC/TDD/DPIA/assessment needs)"),
    "milestone_governance": _a(MILESTONE_GOVERNANCE),
    "rfcs": _a(RFC),
    "tdds": _a(TDD),
    "interface_contracts": _a(INTERFACE_CONTRACT),
    "assumptions": _a(STATEMENT),
    "unknowns": _a(UNKNOWN),
    "material_questions": _a(QUESTION, "Only ambiguities material to architecture/scope/cost/security/legal/target"),
    "facts": _a(STATEMENT),
}
PLAN_SECTIONS: tuple[str, ...] = tuple(k for k in PLAN_CONTENT_PROPERTIES if k != "schema_version")


def plan_content_schema(*, with_header: bool = True) -> dict[str, Any]:
    schema = _o(dict(PLAN_CONTENT_PROPERTIES), "PlanVersion content (Product 1 nodes 00-08 + DAG)")
    if with_header:
        return {"$schema": DRAFT, "title": "aeos.planning.plan-content/1", **schema}
    return schema


FINDING = _o({"key": _s(), "severity": _e(SEVERITIES), "section": _s("Plan section the finding refers to"),
              "description": _s(), "recommendation": _s()})


def challenge_schema() -> dict[str, Any]:
    return {"$schema": DRAFT, "title": "aeos.planning.challenge-findings/1", **_o({
        "summary": _s(),
        "overall_severity": _e(SEVERITIES),
        "contradictions": _a(FINDING),
        "missing_dependencies": _a(FINDING),
        "risks": _a(FINDING),
        "scope_issues": _a(FINDING),
        "other_findings": _a(FINDING),
    }, "Planning Challenger findings for exactly one plan package")}


def synthesis_schema() -> dict[str, Any]:
    return {"$schema": DRAFT, "title": "aeos.planning.synthesis/1", **_o({
        "summary": _s(),
        "content": plan_content_schema(with_header=False),
        "resolutions": _a(_o({"finding_key": _s(),
                              "action": _e(["ADDRESSED", "PARTIALLY_ADDRESSED", "DEFERRED", "REJECTED"]),
                              "note": _s()})),
    }, "Planning Synthesizer output: a NEW plan version plus how each finding was handled")}


def final_review_schema() -> dict[str, Any]:
    return {"$schema": DRAFT, "title": "aeos.planning.final-review/1", **_o({
        "review": _o({"outcome": _s(), "change_summary": _s(), "architecture_impact": _s(), "owner_auth": _s(),
                      "blast_radius": _s(), "baseline": _s(), "contradictions": _sa(), "scope": _s()}),
        "verify": _o({"claims": _a(_o({"claim": _s(), "evidence": _s(),
                                       "status": _e(["SUPPORTED", "UNSUPPORTED", "UNKNOWN"])})),
                      "test_ids": _sa(), "environment": _s(), "old_behavior": _s(), "unknowns": _sa()}),
        "verdict": _e(PLAN_VERDICTS, "A plan package can at most be PASS-STATIC; no tests were executed"),
        "verdict_rationale": _s(),
        "findings": _a(_o({"key": _s(), "severity": _e(SEVERITIES), "description": _s(), "recommendation": _s(),
                           "blocking": _b()})),
    }, "Final Planning Reviewer REVIEW + VERIFY + VERDICT (§34); a verdict is not human approval")}


SCHEMA_BUILDERS = {
    "plan_content": plan_content_schema,
    "challenge_findings": challenge_schema,
    "synthesis": synthesis_schema,
    "final_review": final_review_schema,
}
SCHEMA_FILES = {
    "plan_content": "plan-content.schema.json",
    "challenge_findings": "challenge-findings.schema.json",
    "synthesis": "synthesis.schema.json",
    "final_review": "final-review.schema.json",
}


def schema(name: str) -> dict[str, Any]:
    return SCHEMA_BUILDERS[name]()


def contracts_dir() -> Path:
    return repo_root() / "contracts" / "planning"


def render(name: str) -> str:
    return json.dumps(schema(name), indent=2, ensure_ascii=False) + "\n"


def write_contract_files(directory: Path | None = None) -> list[Path]:
    root = directory or contracts_dir()
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for name, filename in SCHEMA_FILES.items():
        path = root / filename
        path.write_text(render(name), encoding="utf-8", newline="\n")
        out.append(path)
    return out


def upgrade_content(content: Any) -> Any:
    """Content stored before the additive sections existed gets them as empty lists (meaning preserved:
    no RFC/TDD/contract requested, feature owner/personal-data status UNKNOWN). Present sections are kept
    as they are; nothing else is changed. Non-dict input is returned unchanged (schema validation reports it)."""
    if not isinstance(content, dict):
        return content
    missing = [k for k in ADDITIVE_SECTIONS if k not in content]
    out = {**content, **{k: [] for k in missing}} if missing else content
    ops = out.get("operations")
    if isinstance(ops, dict):  # revision 3: nested additive operations fields (empty = not given)
        new_ops = {**{k: json.loads(json.dumps(v)) for k, v in OPERATIONS_ADDITIVE.items() if k not in ops}, **ops}
        backup = new_ops.get("backup")
        if isinstance(backup, dict) and any(k not in backup for k in BACKUP_ADDITIVE):
            new_ops["backup"] = {**backup, **{k: v for k, v in BACKUP_ADDITIVE.items() if k not in backup}}
        books = new_ops.get("runbooks")
        if isinstance(books, list) and any(isinstance(b, dict) and any(k not in b for k in RUNBOOK_ADDITIVE)
                                           for b in books):  # revision 4: per-runbook procedure fields
            new_ops["runbooks"] = [{**{k: v for k, v in RUNBOOK_ADDITIVE.items() if k not in b}, **b}
                                   if isinstance(b, dict) else b for b in books]
        if new_ops != ops:
            out = {**out, "operations": new_ops}
    return out


def empty_plan_content(name: str = "", *, data_classification: str = "INTERNAL") -> dict[str, Any]:
    """A schema-valid skeleton (used by tests/fixtures and as an editing template)."""

    def fill(s: dict[str, Any]) -> Any:
        t = s.get("type")
        if t == "object":
            return {k: fill(v) for k, v in s["properties"].items()}
        if t == "array":
            return []
        if t == "integer":
            return 0
        if t == "boolean":
            return False
        if "enum" in s:
            return s["enum"][0]
        return ""

    content = fill(plan_content_schema(with_header=False))
    content["product_home"]["name"] = name
    content["product_home"]["criticality"] = "UNKNOWN"
    content["product_home"]["data_classification"] = data_classification
    content["operations"]["criticality"] = "UNKNOWN"
    return content
