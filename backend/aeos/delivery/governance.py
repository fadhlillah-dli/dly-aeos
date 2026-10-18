"""GovernanceHarnessAdapter (§32) — handoff after verification/review, before delivery trust promotion.

The name refers to the governance integration contract planned for DLY AEOS, **not** a claim that a
Harness.io repository/service was inspected. Until a real downstream contract (hooks, callers,
consumers) is reviewed, the configured adapter is ``NullGovernanceHarness`` which returns
NOT_CONFIGURED and marks every downstream check BLOCKED.

Guarantees enforced around any adapter:

* the input/output contract is versioned (``CONTRACT_VERSION``) and an adapter for another version is
  refused;
* adapters receive an immutable payload and **no database session** — they cannot change verifier
  results or write human approvals/Decision rows; a payload whose hash changed during the call is
  rejected;
* artifacts whose identity changed since acceptance (different artifact hash, STALE acceptance,
  PR head/base moved) are rejected before the adapter is called;
* the output never counts as human approval and never bypasses an office pipeline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, NotFound
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.verification import acceptance as acceptance_mod
from aeos.verification import collector
from aeos.verification.verifier import related_evidence

CONTRACT_VERSION = "1.0"
RESULTS = ("HANDED_OFF", "ACCEPTED", "REJECTED", "BLOCKED", "NOT_CONFIGURED")
DEPENDENCY_FILES = ("pyproject.toml", "package.json", "package-lock.json", "requirements", "uv.lock", "poetry.lock")
CONTRACT_PATHS = ("facade.py", "contracts/", "backend/aeos/api/", "router.py")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType | dict):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class GovernanceHandoffInput:
    contract_version: str
    project_id: str
    domain: str
    task_id: str
    task_key: str
    task_spec_hash: str | None
    plan_version_id: str
    plan_version_hash: str | None
    acceptance_id: str
    artifact_hash: str
    base_commit: str | None
    head_commit: str | None
    config_hash: str | None
    change_scope: tuple[str, ...]
    dependency_deltas: tuple[str, ...]
    contract_deltas: tuple[str, ...]
    verifier_reports: tuple[Any, ...]
    reviewer: Any
    risk_decisions: tuple[str, ...]
    rollback: Any

    def to_dict(self) -> dict[str, Any]:
        return {k: _thaw(v) for k, v in self.__dict__.items()}

    def digest(self) -> str:
        return content_hash(json.loads(json.dumps(self.to_dict(), default=str)))


@dataclass(frozen=True)
class DownstreamCheck:
    name: str
    status: str  # PASS | FAIL | BLOCKED | PENDING | NOT_APPLICABLE
    reason: str = ""


@dataclass(frozen=True)
class GovernanceHandoffOutput:
    contract_version: str
    adapter: str
    adapter_version: str
    result: str
    input_hash: str
    required_downstream_checks: tuple[DownstreamCheck, ...] = ()
    required_decisions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    handed_off_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_human_approval"] = False
        return d


class GovernanceHarnessAdapter(Protocol):
    name: str
    version: str
    contract_version: str

    def handoff(self, payload: GovernanceHandoffInput) -> GovernanceHandoffOutput: ...


class NullGovernanceHarness:
    """Default adapter: no downstream governance harness is configured (honest NOT_CONFIGURED)."""

    name = "null-governance-harness"
    version = "1"
    contract_version = CONTRACT_VERSION

    def handoff(self, payload: GovernanceHandoffInput) -> GovernanceHandoffOutput:
        return GovernanceHandoffOutput(
            contract_version=CONTRACT_VERSION, adapter=self.name, adapter_version=self.version,
            result="NOT_CONFIGURED", input_hash=payload.digest(),
            required_downstream_checks=(DownstreamCheck(
                "downstream-governance", "BLOCKED",
                "Belum ada governance harness downstream yang dikonfigurasi; kontrak/hook/consumer nyata "
                "(mis. Harness.io atau pipeline kantor) belum diperiksa (§32)."),),
            required_decisions=("Owner: tetapkan dan tinjau kontrak governance downstream sebelum promosi kepercayaan",),
            reasons=("NOT_CONFIGURED",),
        )


_adapter: GovernanceHarnessAdapter = NullGovernanceHarness()


def configured_adapter() -> GovernanceHarnessAdapter:
    return _adapter


def build_input(session: Session, task_id: str) -> tuple[GovernanceHandoffInput, Any]:
    task = session.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    project = session.get(Project, task.project_id)
    if project is None:
        raise NotFound("project not found")
    acc = acceptance_mod.latest_acceptance(session, task_id)
    if acc is None or acc.acceptance_status != acceptance_mod.ACCEPTED:
        raise Blocked("governance handoff requires an ACCEPTED acceptance record", code="ACCEPTANCE_REQUIRED")
    report = session.get(EvidenceArtifact, acc.verification_evidence_id) if acc.verification_evidence_id else None
    full = (collector.read_json(report) or collector.evidence_summary(report)) if report else {}
    scope_ev = related_evidence(session, report, "SCOPE_CHECK")
    changed = list((collector.read_json(scope_ev) or {}).get("changed_files") or [])
    review = session.get(ReviewRecord, acc.review_record_id) if acc.review_record_id else None
    migrations = [p for p in changed if "/migrations/" in p]
    payload = GovernanceHandoffInput(
        contract_version=CONTRACT_VERSION, project_id=project.id, domain=project.domain, task_id=task.id,
        task_key=task.task_key, task_spec_hash=task.spec_hash, plan_version_id=task.plan_version_id,
        plan_version_hash=task.plan_version_hash, acceptance_id=acc.id, artifact_hash=acc.artifact_hash,
        base_commit=acc.base_commit, head_commit=acc.head_commit, config_hash=acc.config_hash,
        change_scope=tuple(changed),
        dependency_deltas=tuple(p for p in changed if any(p.endswith(d) or d in p for d in DEPENDENCY_FILES)),
        contract_deltas=tuple(p for p in changed if any(c in p for c in CONTRACT_PATHS)),
        verifier_reports=_freeze([{"evidence_id": report.id, "content_hash": report.content_hash,
                                   "status": full.get("status"), "level": report.evidence_level}] if report else []),
        reviewer=_freeze({"review_record_id": review.id, "verdict": review.verdict,
                          "findings": review.findings} if review else {}),
        risk_decisions=tuple(),
        rollback=_freeze({"migrations_changed": migrations,
                          "note": "code revert does not revert data" if migrations else "revert the merge commit"}),
    )
    return payload, acc


def integrity_problems(session: Session, payload: GovernanceHandoffInput, acc: Any,
                       artifact_hash: str | None) -> list[str]:
    problems: list[str] = []
    if artifact_hash is not None and artifact_hash != acc.artifact_hash:
        problems.append("artifact hash changed since acceptance; promotion refused")
    if payload.artifact_hash != acc.artifact_hash or payload.head_commit != acc.head_commit:
        problems.append("handoff payload does not match the accepted artifact identity")
    if acc.acceptance_status != acceptance_mod.ACCEPTED:
        problems.append(f"acceptance is {acc.acceptance_status}")
    problems += [f"acceptance stale: {s}" for s in acceptance_mod.staleness(session, acc)]
    pr = session.scalar(select(PullRequestRecord).where(PullRequestRecord.acceptance_id == acc.id)
                        .order_by(PullRequestRecord.created_at.desc()).limit(1))
    if pr is not None and pr.observed_head_sha and pr.observed_head_sha != acc.head_commit:
        problems.append("PR head differs from the accepted head commit")
    return sorted(set(problems))


def handoff(session: Session, task_id: str, *, actor: str, adapter: GovernanceHarnessAdapter | None = None,
            artifact_hash: str | None = None) -> dict[str, Any]:
    """Run the governance handoff for an accepted task and record the result as evidence (caller commits)."""
    adapter = adapter or configured_adapter()
    payload, acc = build_input(session, task_id)
    before = payload.digest()
    problems = integrity_problems(session, payload, acc, artifact_hash)
    if getattr(adapter, "contract_version", None) != CONTRACT_VERSION:
        problems.append(f"adapter contract {getattr(adapter, 'contract_version', None)} != {CONTRACT_VERSION}")
    if problems:
        output = GovernanceHandoffOutput(contract_version=CONTRACT_VERSION, adapter=getattr(adapter, "name", "?"),
                                         adapter_version=getattr(adapter, "version", "?"), result="REJECTED",
                                         input_hash=before, reasons=tuple(problems))
    else:
        output = adapter.handoff(payload)
        after = payload.digest()
        bad: list[str] = []
        if after != before:
            bad.append("adapter mutated the handoff payload")
        if output.input_hash != before:
            bad.append("adapter output is bound to a different payload hash")
        if output.contract_version != CONTRACT_VERSION:
            bad.append("adapter output uses another contract version")
        if output.result not in RESULTS:
            bad.append(f"unknown adapter result {output.result!r}")
        if bad:
            output = GovernanceHandoffOutput(contract_version=CONTRACT_VERSION, adapter=output.adapter,
                                             adapter_version=output.adapter_version, result="REJECTED",
                                             input_hash=before, reasons=tuple(bad))
    project = session.get(Project, payload.project_id)
    ev = collector.collect_evidence(
        session, kind="GOVERNANCE_HANDOFF", origin="collector", collector_identity=f"governance:{output.adapter}"[:128],
        claim=f"Governance handoff for task {payload.task_key}: {output.result}",
        content={"input": payload.to_dict(), "output": output.to_dict()}, evidence_level="E1", subject_type="task",
        subject_id=task_id, project_id=payload.project_id, domain=project.domain if project else None,
        task_id=task_id, environment={"commit": payload.head_commit, "artifact_hash": payload.artifact_hash,
                                      "contract_version": CONTRACT_VERSION},
    )
    audit.record(session, actor_type="user", actor_id=actor, action="delivery.governance.handoff",
                 result="SUCCESS" if output.result not in ("REJECTED",) else "DENIED", target_type="task",
                 target_id=task_id, project_id=payload.project_id, domain=payload.domain,
                 reason="; ".join(output.reasons)[:500], details={"result": output.result, "evidence_id": ev.id})
    events.emit(session, source="delivery", kind="governance.handoff", entity_type="task", entity_id=task_id,
                project_id=payload.project_id, domain=payload.domain,
                payload={"result": output.result, "evidence_id": ev.id})
    return {"input_hash": before, "output": output.to_dict(), "evidence_id": ev.id}
