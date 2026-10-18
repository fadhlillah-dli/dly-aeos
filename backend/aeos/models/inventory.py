"""InstallManifest, ComponentReference, ComponentObservation, typed relations (§13, §36-§38).

Desired (manifest/decision) vs configured vs observed are separate records; observations never
grant permission and never rewrite desired state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class InstallManifestRecord(IdMixin, CreatedMixin, Base):
    __tablename__ = "install_manifests"

    manifest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_path: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    loaded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ComponentReference(TimestampMixin, Base):
    __tablename__ = "component_references"

    component_id: Mapped[str] = mapped_column(String(96), primary_key=True)  # stable id, e.g. "claude-code"
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    friendly_name: Mapped[str] = mapped_column(String(200), default="")  # "OpenCode — mesin eksekusi pekerja"
    function: Mapped[str] = mapped_column(Text, default="")
    why_selected: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")  # map fold domain (§36)
    source_kind: Mapped[str] = mapped_column(String(48), default="UNKNOWN")  # GITHUB_REPO|OFFICIAL_DOCS|...
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # S01..S64
    upstream_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    identity_verified: Mapped[str] = mapped_column(String(16), default="UNVERIFIED")
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # platform/project, domains, env
    owner: Mapped[str] = mapped_column(String(128), default="owner")
    usage_role: Mapped[str] = mapped_column(String(32), default="runtime")  # runtime|build|dev|test|reference
    requirement: Mapped[str] = mapped_column(String(32), default="CANDIDATE")  # REQUIRED|OPTIONAL|CANDIDATE|REFERENCE_ONLY
    design_label: Mapped[str] = mapped_column(String(32), default="CANDIDATE")
    approved_pin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bootstrap_order: Mapped[int | None] = mapped_column(nullable=True)
    # §38 dimensions (desired/decision side); observed dimensions come from observations.
    admission_status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    enablement: Mapped[str] = mapped_column(String(16), default="DISABLED")
    lifecycle_final: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    probe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # health/version probe contract
    capability_tests: Mapped[list[Any]] = mapped_column(JSON, default=list)
    access: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # tools/paths/egress/data class
    cost: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    operations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # restart/uninstall/recovery notes
    decision_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    manifest_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(nullable=True)  # removal keeps history
    mode: Mapped[str] = mapped_column(String(16), default="DESIGN")
    # §45 placement decision text, verbatim intent from the master catalog.
    placement: Mapped[str] = mapped_column(Text, default="")
    # Manifest extras that have no dedicated column (discovery hints, invalidators, notes,
    # source_checked_on, maintainer). Never secrets; never installation/usage evidence.
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ComponentObservation(IdMixin, Base):
    __tablename__ = "component_observations"
    __table_args__ = (Index("ix_obs_component_time", "component_id", "observed_at"),)

    component_id: Mapped[str] = mapped_column(String(96), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)  # installation|configuration|connectivity|functional|usage
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observer: Mapped[str] = mapped_column(String(64), nullable=False)  # doctor|broker|runner|reconciler
    observed_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    observed_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_location: Mapped[str | None] = mapped_column(String(512), nullable=True)  # redacted/logical
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    operation_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ComponentRelation(IdMixin, TimestampMixin, Base):
    __tablename__ = "component_relations"
    __table_args__ = (UniqueConstraint("source_id", "target_id", "relation", "lens"),)

    source_id: Mapped[str] = mapped_column(String(96), nullable=False)
    target_id: Mapped[str] = mapped_column(String(96), nullable=False)
    # depends_on|calls|reads|writes|publishes_to|verifies|stores_in|runs_with|implements|governed_by|replaces
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    lens: Mapped[str] = mapped_column(String(16), nullable=False)  # DESIGN|CONFIG|OBSERVED
    provenance: Mapped[str] = mapped_column(String(256), default="")  # source record/artifact
    data_description: Mapped[str] = mapped_column(Text, default="")  # what flows, direction, read/write
    boundary: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_observed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # READ|WRITE|READ_WRITE|CALL|EXEC|NONE — what the edge does (§36 "operasi read/write").
    operation: Mapped[str] = mapped_column(String(16), default="")
    # Logical identity/credential reference used across the edge (never a value).
    identity_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # A relation dropped from the manifest is tombstoned, not deleted (§38 history).
    tombstoned_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CheckResult(IdMixin, Base):
    """§08 Readiness Doctor result record."""

    __tablename__ = "check_results"
    __table_args__ = (Index("ix_check_results_check_time", "check_id", "last_checked_at"),)

    check_id: Mapped[str] = mapped_column(String(96), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="")
    group: Mapped[str] = mapped_column(String(48), nullable=False)
    component_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    component_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # project/domain/capability
    target_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    summary: Mapped[str] = mapped_column(Text, default="")
    observed_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_checked_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    invalidators: Mapped[list[str]] = mapped_column(JSON, default=list)
    fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)  # binary/config identity hash
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remediation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requires_destructive: Mapped[bool] = mapped_column(default=False)
    requires_paid_write: Mapped[bool] = mapped_column(default=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # doctor run grouping
