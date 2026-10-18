"""Inventory reconciliation runs and drift findings (§36, §38). Owned by ``aeos.inventory``.

A ReconciliationRun records one auditable pass (desired manifest -> read-only probes ->
observations -> comparison). Partial runs keep per-component status and time; drift findings are
persisted projections that are resolved (never deleted) when a later run no longer sees them.

This file also guards ``architecture_snapshots`` (§13: ArchitectureSnapshot is an immutable
artifact) with append-only triggers whenever the table is created. The trigger names match the
ones ``aeos.core.db.install_append_only_triggers`` would use so they are never duplicated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DDL, JSON, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from aeos.core.db import Base
from aeos.core.timeutil import utcnow
from aeos.models.base import IdMixin
from aeos.models.decisions import ArchitectureSnapshot


class ReconciliationRun(IdMixin, Base):
    __tablename__ = "inventory_reconciliation_runs"
    __table_args__ = (Index("ix_reconcile_started", "started_at"),)

    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="cli")  # cli|api|seed|schedule
    actor: Mapped[str] = mapped_column(String(64), default="system")
    # {"component_id": "..."} for a scoped refresh or {"set": "core"} for the core set.
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")  # RUNNING|COMPLETED|PARTIAL|FAILED
    # component_id -> {"status": OK|FAILED|UNKNOWN|SKIPPED, "at": iso, "reason": str, "observations": n}
    component_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Coverage of managed-scope sources read in this run (lockfiles, workflows, configs).
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    observations_recorded: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="LIVE")


class DriftFinding(IdMixin, Base):
    """One drift class for one component (or unregistered item) between two lenses."""

    __tablename__ = "inventory_drift_findings"
    __table_args__ = (
        Index("ix_drift_component", "component_id", "drift_class"),
        Index("ix_drift_status", "status"),
    )

    component_id: Mapped[str] = mapped_column(String(160), nullable=False)
    # planned-but-missing|installed-but-disabled|enabled-but-unverified|resolved-version-mismatch|
    # observed-but-unregistered|obsolete-reference
    drift_class: Mapped[str] = mapped_column(String(40), nullable=False)
    finding_key: Mapped[str] = mapped_column(String(256), default="")  # dedupe key within a class
    severity: Mapped[str] = mapped_column(String(16), default="LOW")
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lens_from: Mapped[str] = mapped_column(String(16), default="DESIGN")
    lens_to: Mapped[str] = mapped_column(String(16), default="OBSERVED")
    domain_scope: Mapped[str] = mapped_column(String(16), default="PLATFORM")  # PLATFORM|PERSONAL|OFFICE
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN|RESOLVED
    first_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    first_detected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_detected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


_SNAPSHOT_TABLE = ArchitectureSnapshot.__table__
for _op in ("update", "delete"):
    event.listen(
        _SNAPSHOT_TABLE,
        "after_create",
        DDL(
            f"CREATE TRIGGER IF NOT EXISTS trg_architecture_snapshots_no_{_op} BEFORE {_op.upper()} "
            "ON architecture_snapshots BEGIN SELECT RAISE(ABORT, 'architecture_snapshots is append-only'); END"
        ).execute_if(dialect="sqlite"),
    )
