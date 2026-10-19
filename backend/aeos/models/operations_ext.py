"""Operations & Reliability records (§33, §35, §41). Owned by ``aeos.operations``.

* ``OpsAlert`` — deduplicated alert state; every new/re-opened alert is also emitted as an
  ``alert.raised`` event (the event stream stays the dashboard source, this table only dedups).
* ``OpsExportSnapshot`` — registry of exported snapshots (id, hash, scope, time). An export is a
  snapshot, never a live view (§41); the registry lets a human verify an export file later.

Backups and releases are recorded on disk next to their artifacts (manifest/provenance files) so a
database restore can never make the recovery points themselves disappear.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import CreatedMixin, IdMixin, TimestampMixin


class OpsAlert(IdMixin, Base):
    __tablename__ = "ops_alerts"
    __table_args__ = (Index("ix_ops_alerts_status", "status"), Index("ix_ops_alerts_domain", "domain"))

    dedup_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # stale_leases|sli_breach|restore_untested|...
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # redacted
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None = platform-level
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="sli")  # sli|release|backup|emergency
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN|RESOLVED
    auto_resolve: Mapped[bool] = mapped_column(Boolean, default=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")


class OpsIncident(IdMixin, TimestampMixin, Base):
    """Incident record (§28 incident documentation, §33 incident response, §16 Incident Analyst).

    Opened by an emergency stop (or by an operator for an alert). Tracks owner, declared and derived
    blast radius, timeline, impact, hypotheses, the evidenced root cause, corrective actions and the
    regression test / eval case that must exist before the incident can be RESOLVED. Free text is
    redacted before it is stored. ``derived_blast_radius`` is keyed by domain so Office details never
    reach a Personal view."""

    __tablename__ = "ops_incidents"
    __table_args__ = (Index("ix_ops_incidents_status", "status"),)

    source: Mapped[str] = mapped_column(String(32), default="emergency_stop")  # emergency_stop|alert|manual
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # emergency incident id / alert id
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN|CONTAINED|RESOLVED
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None = platform-wide
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    declared_blast_radius: Mapped[str] = mapped_column(Text, default="")
    derived_blast_radius: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timeline: Mapped[list[Any]] = mapped_column(JSON, default=list)
    impact: Mapped[str] = mapped_column(Text, default="")
    hypotheses: Mapped[list[Any]] = mapped_column(JSON, default=list)
    root_cause: Mapped[str] = mapped_column(Text, default="")
    root_cause_evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    root_cause_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # proposal/TaskSpec id
    corrective_actions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    regression_test_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    eval_case_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    follow_ups: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class OpsRetirement(TimestampMixin, Base):
    """Tracked retirement workflow of one project (§10 retirement workflow, §33): per-step status,
    owner and evidence ids, the checklist hash an owner RETIREMENT decision must bind to, and the
    legal-hold state. Nothing here deletes data, services or backups (§16 Retirement Assistant)."""

    __tablename__ = "ops_retirements"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="IN_PROGRESS")  # IN_PROGRESS|RETIRED
    steps: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # key -> {status, owner, evidence_ids, note}
    checklist_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    legal_hold: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # {active, reason, actor, at}
    updated_by: Mapped[str] = mapped_column(String(128), default="")


class OpsSliSample(IdMixin, Base):
    """One measured SLI value at an evaluation (§13 Metric with redacted dimensions, §46 error budget).

    Samples are the measurement history the error budget is computed from; dimensions are only the
    domain/scope and the owner-assigned criticality (no free text, no identifiers)."""

    __tablename__ = "ops_sli_samples"
    __table_args__ = (Index("ix_ops_sli_samples_key_at", "sli_key", "at"),)

    at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    sli_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # PERSONAL|OFFICE|PLATFORM
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # NO_TARGET|UNKNOWN|WITHIN_TARGET|BREACH


class OpsExportSnapshot(IdMixin, CreatedMixin, Base):
    __tablename__ = "ops_export_snapshots"
    __table_args__ = (Index("ix_ops_export_kind", "kind"),)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # inventory|architecture|evidence|audit|decisions
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    redactions_applied: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
