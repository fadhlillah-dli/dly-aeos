"""Shared helpers for planning/policy tests (no tests here).

FakeBridge replaces the ClaudeNativeBridge facade functions; it never calls a model or any API.
Outputs are labelled MOCK (evidence honesty).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aeos.claude_bridge import facade as bridge_facade
from aeos.core.enums import Domain
from aeos.core.errors import Blocked
from aeos.core.ids import new_id
from aeos.models.portfolio import Project
from aeos.planning import contracts

PURPOSE_STAGE = {
    "planning.primary": "PRIMARY_PLANNING",
    "planning.challenge": "CHALLENGE_REVIEW",
    "planning.synthesis": "SYNTHESIS",
    "planning.final": "FINAL_REVIEW",
}
RESOLVED = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1"}


def make_project(db: Session, *, domain: Domain = Domain.PERSONAL, key: str | None = None, kind: str = "NEW",
                 is_platform: bool = False, classification: str = "INTERNAL", owner_id: str | None = None) -> Project:
    key = key or f"proj-{new_id()[:8]}"
    p = Project(project_key=key, name=f"Project {key}", domain=domain.value, project_kind=kind,
                is_platform=is_platform, data_classification=classification, owner_user_id=owner_id)
    db.add(p)
    db.commit()
    return p


def task(key: str, *, deps: list[str] | None = None, paths: list[str] | None = None, feature: str = "F1",
         **overrides: Any) -> dict[str, Any]:
    t = {
        "key": key, "feature_key": feature, "title": f"Task {key}", "goal": f"Implement {key}",
        "role_key": "feature-implementer", "input_artifacts": ["PRD F1"], "expected_outputs": ["code", "tests"],
        "acceptance_criteria": [f"{key} works"], "depends_on": deps or [],
        "allowed_paths": paths if paths is not None else [f"src/{key.lower()}/**"], "forbidden_paths": [],
        "tools_allowed": [], "egress_allowed": [],
        "capability_scope": {"operations": [], "paths": [], "notes": ""}, "capability_ttl_seconds": 0,
        "data_classification": "INTERNAL",
        "model_route_policy": {"pool": "WORKER", "requires_tools": True, "min_context_tokens": 0, "notes": ""},
        "reviewer_policy": {"required": True, "reviewer_role": "", "checkpoint": ""},
        "verifier_policy": {"required": True, "suites": [], "negative_tests_required": True},
        "limits": {"max_attempts": 2, "timeout_seconds": 1800, "max_output_tokens": 16000},
        "stop_conditions": [], "evidence_requirements": [], "risk_class": "R1", "artifact_schema_version": "1",
    }
    t.update(overrides)
    return t


def sample_content(name: str = "Demo", *, tasks: list[dict[str, Any]] | None = None, note: str = "") -> dict[str, Any]:
    c = contracts.empty_plan_content(name)
    c["overview"]["overview"] = f"Ringkasan {name} {note}".strip()
    c["milestones"] = [{"key": "M1", "title": "Slice pertama", "exit_evidence": "verifier report", "order": 1}]
    c["features"] = [
        {"key": "F1", "title": "Fitur satu", "requirement": "R1", "priority": "P1", "acceptance": ["AC1"],
         "depends_on": [], "milestone": "M1"},
        {"key": "F2", "title": "Fitur dua", "requirement": "R2", "priority": "P2", "acceptance": ["AC2"],
         "depends_on": ["F1"], "milestone": "M1"},
    ]
    c["tasks"] = tasks if tasks is not None else [task("T1"), task("T2", deps=["T1"], feature="F2")]
    c["requirements"] = [{
        "feature_key": "F1", "title": "PRD F1", "overview": "", "problem": "", "background": "", "goals": [],
        "non_goals": [], "business_requirements": [], "functional_requirements": [], "non_functional_requirements": [],
        "user_stories": [], "acceptance_criteria": ["AC1"], "user_impact": "", "dependencies": [], "assumptions": [],
        "risks": [], "success_metrics": [], "rollout_considerations": "", "related_work": [],
    }]
    c["architecture"]["diagrams"] = [{"key": "D1", "title": "Context", "kind": "context",
                                      "mermaid": "flowchart TD\n  A-->B", "verification_source": "contract"}]
    # §41 readiness: the PROJECT_CREATION required sections (scope, context, authn/authz, risk register) are
    # stated, so implementation of this plan is not held by documentation pending
    c["overview"]["scope_in"] = ["Fitur pertama end-to-end"]
    c["security_privacy"]["overview"]["authn_authz"] = "Sesi lokal; satu peran pemilik"
    c["security_privacy"]["initial_risks"] = [{"key": "R1", "title": "Kehilangan data", "description": "File DB rusak",
                                              "likelihood": "LOW", "impact": "MEDIUM", "mitigation": "Backup harian",
                                              "residual_risk": "Rendah", "owner": "UNKNOWN"}]
    return c


def challenge_output(**overrides: Any) -> dict[str, Any]:
    out = {"summary": "1 temuan", "overall_severity": "LOW", "contradictions": [],
           "missing_dependencies": [{"key": "C1", "severity": "LOW", "section": "tasks",
                                     "description": "T2 butuh kontrak API", "recommendation": "tambahkan AC"}],
           "risks": [], "scope_issues": [], "other_findings": []}
    out.update(overrides)
    return out


def synthesis_output(content: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "temuan C1 ditangani", "content": content or sample_content(note="v2"),
            "resolutions": [{"finding_key": "C1", "action": "ADDRESSED", "note": "AC ditambah"}]}


def final_output(verdict: str = "PASS-STATIC", findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "review": {"outcome": "ok", "change_summary": "", "architecture_impact": "", "owner_auth": "owner",
                   "blast_radius": "", "baseline": "", "contradictions": [], "scope": ""},
        "verify": {"claims": [{"claim": "DAG valid", "evidence": "validator", "status": "SUPPORTED"}], "test_ids": [],
                   "environment": "", "old_behavior": "", "unknowns": []},
        "verdict": verdict, "verdict_rationale": "static review only", "findings": findings or [],
    }


class FakeBridge:
    """Monkeypatches the bridge facade. ``blocked``: stages raising Blocked; ``quota``: stages returning
    quota_exhausted; ``models``: per-stage resolved model override; ``outputs``: per-stage structured."""

    def __init__(self, monkeypatch: Any, tmp_path: Path, *, blocked: set[str] | None = None,
                 quota: set[str] | None = None, models: dict[str, str | None] | None = None,
                 outputs: dict[str, Any] | None = None, sessions: dict[str, str] | None = None,
                 manual_models: dict[str, str | None] | None = None) -> None:
        self.calls: list[Any] = []
        self.packages: dict[str, Any] = {}
        self.manual_results: dict[str, Any] = {}
        self.blocked = set(blocked or ())
        self.quota = set(quota or ())
        self.models = dict(models or {})
        self.outputs = dict(outputs or {})
        self.sessions = dict(sessions or {})
        self.manual_models = dict(manual_models or {})
        self.tmp_path = tmp_path
        monkeypatch.setattr(bridge_facade, "run_structured", self.run_structured)
        monkeypatch.setattr(bridge_facade, "write_manual_package", self.write_manual_package)
        monkeypatch.setattr(bridge_facade, "read_manual_result", self.read_manual_result)

    def stage_calls(self) -> list[str]:
        return [PURPOSE_STAGE[c.purpose] for c in self.calls]

    def _output(self, stage: str) -> Any:
        if stage in self.outputs:
            return copy.deepcopy(self.outputs[stage])
        return {"PRIMARY_PLANNING": sample_content(), "CHALLENGE_REVIEW": challenge_output(),
                "SYNTHESIS": synthesis_output(), "FINAL_REVIEW": final_output()}[stage]

    def _result(self, req: Any, stage: str, model: str | None, *, mode: str = "MOCK") -> Any:
        return bridge_facade.ClaudeResult(
            ok=True, structured=self._output(stage), text="", model_requested=req.model, model_resolved=model,
            engine_version="2.1.301 (fake)", session_id=self.sessions.get(stage, f"sess-{len(self.calls)}-{stage}"),
            usage={"input_tokens": 100, "output_tokens": 50}, duration_ms=7, mode=mode)

    def run_structured(self, req: Any) -> Any:
        self.calls.append(req)
        stage = PURPOSE_STAGE[req.purpose]
        if stage in self.blocked:
            raise Blocked("ClaudeNativeBridge not proven", code="BRIDGE_NOT_PROVEN")
        if stage in self.quota:
            return bridge_facade.ClaudeResult(ok=False, structured=None, text="", model_requested=req.model,
                                              model_resolved=None, engine_version="2.1.301", session_id=None,
                                              usage={}, duration_ms=1, error_code="QUOTA_EXHAUSTED",
                                              error="weekly limit reached", quota_exhausted=True)
        model = self.models[stage] if stage in self.models else RESOLVED[req.model]
        return self._result(req, stage, model)

    def write_manual_package(self, req: Any, *, package_id: str) -> Path:
        path = self.tmp_path / "manual" / package_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "prompt.md").write_text(req.prompt, encoding="utf-8")
        self.packages[package_id] = req
        return path

    def complete_manual(self, package_id: str) -> None:
        req = self.packages[package_id]
        stage = PURPOSE_STAGE[req.purpose]
        model = self.manual_models[stage] if stage in self.manual_models else RESOLVED[req.model]
        self.manual_results[package_id] = self._result(req, stage, model, mode="MANUAL_FALLBACK")

    def read_manual_result(self, package_id: str) -> Any:
        return self.manual_results.get(package_id)
