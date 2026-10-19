"""Research & Technology Registry, ChangeProposal, evaluation (§39, §40, §43, §44, §47).

ResearchEntry / ChangeProposal / EvalSuite / EvalRun are owned by module group H (research,
proposals, evaluation). ContextPack / MemoryRecord are owned by the knowledge module (§24).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.models.base import IdMixin, TimestampMixin


class ResearchEntry(IdMixin, TimestampMixin, Base):
    """§47 registry entry. Research lifecycle is NOT installation/connection/usage evidence; the
    §38 inventory dimensions stay separate (component_id links to the inventory record)."""

    __tablename__ = "research_entries"
    __table_args__ = (Index("ix_research_identity", "identity"),)

    # Stable seed/registry key (e.g. "tool:opencode", "ref:S28"); unique so seeding is idempotent.
    entry_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    release_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    research_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    maintainer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claims: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_level: Mapped[str] = mapped_column(String(4), default="E0")
    relevance: Mapped[str] = mapped_column(Text, default="")
    placement: Mapped[str] = mapped_column(Text, default="")  # §45 placement decision
    data_terms: Mapped[str] = mapped_column(Text, default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    compatibility: Mapped[str] = mapped_column(Text, default="")
    test_results: Mapped[list[Any]] = mapped_column(JSON, default=list)
    owner_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # DISCOVERED|SOURCE_VERIFIED|CONTROLLED_EVAL|CANARY|RECOMMENDED_FOR_PROMOTION|REJECTED|DEFERRED|ENABLED|REFERENCE_ONLY
    lifecycle: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    checked_at: Mapped[datetime | None] = mapped_column(nullable=True)  # last live upstream check (None = NOT CHECKED)
    component_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # GITHUB_REPO|OFFICIAL_DOCS|PAPER|BLOG|FORUM|PRODUCT|INTERNAL|PRACTICE|UNKNOWN
    source_kind: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    # STATED_BY_MASTER|CONFIGURED|INTERNAL|UNVERIFIED|CHECKED|CHANGED|NOT_FOUND (identity of the source, not trust)
    identity_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED")
    # NOT_CHECKED|OK|IDENTITY_CHANGED|ARCHIVED|NOT_FOUND|RATE_LIMITED|ERROR|NOT_SUPPORTED
    upstream_status: Mapped[str] = mapped_column(String(32), default="NOT_CHECKED")
    upstream: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # fetched metadata (dated)
    history: Mapped[list[Any]] = mapped_column(JSON, default=list)  # identity changes/checks, append-only in code
    section_refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # master sections (§45, §47, §52)
    submitted_domain: Mapped[str | None] = mapped_column(String(16), nullable=True)  # session domain of add-by-URL
    added_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")  # seeded rows are design catalog
    # --- 0002 (§39 item 2, §47, §52): owner-recorded distribution channel (npm/pypi/binary/container digest),
    # the date the terms/privacy page was last read, and a dated manual review of a non-GitHub source.
    distribution: Mapped[str] = mapped_column(String(256), default="")
    terms_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ChangeProposal(IdMixin, TimestampMixin, Base):
    """§39-§41 proposal inside the existing Decision/ADR flow. Status here never replaces task
    execution state or capability readiness (§40)."""

    __tablename__ = "change_proposals"
    __table_args__ = (Index("ix_proposals_domain_status", "domain", "status"),)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # ADD|REPLACE|UPGRADE|REMOVE|COMPARE
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), default="PLATFORM")  # PLATFORM|PROJECT
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # URLs or names
    problem: Mapped[str] = mapped_column(Text, default="")
    expected_benefit: Mapped[str] = mapped_column(Text, default="")
    existing_component_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_component_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # cost/data/resource
    baseline_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    candidate_pin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # SUBMITTED|UNDER_REVIEW|REVIEWED|TRIAL_APPROVED|IN_TRIAL|PROMOTION_PENDING|PROMOTION_APPROVED|ACCEPTED|
    # ROLLED_BACK|FAILED|PARTIAL|KEEP|REJECTED|DEFERRED|REQUEST_MORE_EVIDENCE|REFERENCE_ONLY
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTED")
    review_package: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # §39 items 1-10
    decision_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Canonical source identities (github:owner/repo, url:host/path, name:slug) used for duplicates.
    canonical_identities: Mapped[list[str]] = mapped_column(JSON, default=list)
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parts: Mapped[list[Any]] = mapped_column(JSON, default=list)  # parent: explicit child parts + dependencies
    unknown_fields: Mapped[list[str]] = mapped_column(JSON, default=list)  # technical fields stored as UNKNOWN
    research_entry_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # --- 0002 (§39/§40/§41): owner-declared plan (acceptance criteria before the trial, replacement/
    # cutover/rollback plan, REMOVE closure, canary), part of the candidate hash so an edit re-binds.
    owner_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Links validated per domain: publication/release/eval-run/observation/ADR/plan records (§40, §41).
    links: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Trial launched under the TRIAL decision: plan/version/task ids, caps, status, stop record (§40).
    trial: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Claude checkpoint review of the attached evidence, bound to candidate hash + evidence set (§39, §40).
    checkpoint_review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvalSuite(IdMixin, TimestampMixin, Base):
    """§43/§44 protected, held-out suite definition. Seeded rows hold cases + acceptance only —
    never results. Workers cannot modify graders (no write API)."""

    __tablename__ = "eval_suites"

    suite_key: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_class: Mapped[str] = mapped_column(String(64), default="")  # bugfix|feature-fe-api|docs|...
    version: Mapped[str] = mapped_column(String(32), default="1")
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    held_out: Mapped[bool] = mapped_column(default=True)
    cases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    protected: Mapped[bool] = mapped_column(default=True)  # workers cannot modify graders
    description: Mapped[str] = mapped_column(Text, default="")
    acceptance: Mapped[list[Any]] = mapped_column(JSON, default=list)  # suite-level acceptance criteria
    isolation: Mapped[str] = mapped_column(Text, default="")
    verifier: Mapped[str] = mapped_column(String(128), default="independent-verifier")
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")


class EvalRun(IdMixin, TimestampMixin, Base):
    """One planned/executed evaluation of a full AIConfiguration on a suite (§43). Failed, timeout
    and quota-interrupted samples are kept in ``samples``/``failures`` — never dropped."""

    __tablename__ = "eval_runs"
    __table_args__ = (Index("ix_eval_runs_suite_config", "suite_key", "configuration_hash"),)

    suite_key: Mapped[str] = mapped_column(String(96), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # full AIConfiguration
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    topology: Mapped[str | None] = mapped_column(String(32), nullable=True)  # A|B|C|D (§44)
    # PLANNED|RUNNING|COMPLETED|FAILED|CANCELLED
    status: Mapped[str] = mapped_column(String(32), default="PLANNED")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # §43 dimensions (computed by code)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list)  # never dropped
    gate_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repetitions: Mapped[int] = mapped_column(Integer, default=1)
    samples: Mapped[list[Any]] = mapped_column(JSON, default=list)  # per-sample observations from the verifier
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # declared budget/attempt/time caps
    planned_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_source: Mapped[str | None] = mapped_column(String(64), nullable=True)  # verifier|collector identity
    notes: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")
    # Session domain the run was planned from (None = platform/facade). Runs planned in OFFICE are never
    # listed or ranked in a PERSONAL view (§23, §35); suites themselves are generic platform definitions.
    submitted_domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # --- execution / comparison (§23 "telah diuji", §40 controlled comparison, §43 protocol, §44) -----------
    # ModelRoute.config_hash of the registered provider route when the run was planned: activation evidence
    # (routing) only counts a run made on exactly the configuration being approved.
    route_config_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)  # §43 "baseline ... ditetapkan"
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # §40 controlled comparison link
    # Hash of the declared limits + suite content + environment: only runs under equal conditions are ranked
    # against each other (§43 "environment/input/resource/repair budget setara").
    conditions_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # os/python/controller/harness
    # Case-slot bindings (case x repetition -> executed TaskSpec) used by the trusted collector.
    bindings: Mapped[list[Any]] = mapped_column(JSON, default=list)


class EvalCaseCandidate(IdMixin, TimestampMixin, Base):
    """§28 "failures become regression/eval cases": a CANDIDATE case derived from an observed failure.
    It is never used by an evaluation until a human PROMOTION decision adopts it into its own protected,
    held-out regression suite (a new suite: existing protected suites are never edited)."""

    __tablename__ = "eval_case_candidates"
    __table_args__ = (Index("ix_eval_case_candidates_source", "source_type", "source_id"),)

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # evidence|worker_run|alert
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_suite_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    case: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # redacted case definition
    case_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="CANDIDATE")  # CANDIDATE|ADOPTED|REJECTED
    proposed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    adopted_suite_key: Mapped[str | None] = mapped_column(String(96), nullable=True)


class ContextPack(IdMixin, TimestampMixin, Base):
    """§24 curated task context with provenance and budget."""

    __tablename__ = "context_packs"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items: Mapped[list[Any]] = mapped_column(JSON, default=list)  # {kind, ref, provenance, classification}
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    pack_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stale: Mapped[bool] = mapped_column(default=False)


class MemoryRecord(IdMixin, TimestampMixin, Base):
    """§24 accepted fact/decision with scope, AUTH, expiry and supersession; never secrets."""

    __tablename__ = "memory_records"

    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), default="PROJECT")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    auth_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")  # PROPOSED|ACCEPTED|EXPIRED|SUPERSEDED|REJECTED
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
