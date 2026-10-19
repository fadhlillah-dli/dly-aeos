"""Platform misc (cluster C8): incident records, retirement workflow, single-use recovery credential,
AuditEvent run id, ContextPack AUTH reference and status.

* ``ops_incidents`` - §28/§33 incident record (owner, blast radius, timeline, evidenced root cause,
  corrective action, regression/eval case before RESOLVED).
* ``ops_retirements`` - §10/§33 tracked retirement workflow (steps, checklist hash, legal hold).
* ``ops_sli_samples`` - §13 Metric / §46 measured SLI history for error-budget accounting.
* ``users.temp_password_expires_at`` / ``temp_password_used_at`` - §26 recovery credential is
  single-use and time-limited.
* ``audit_events.run_id`` - §13 AuditEvent actor/run/target.
* ``context_pack_meta.auth_ref`` / ``status`` - §13 ContextPack AUTH + status.

Revision ID: 0005_c8_platform_misc
Revises: 0004_c7_workforce_planning
Create Date: 2026-10-19 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import aeos.core.db

revision = "0005_c8_platform_misc"
down_revision = "0004_c7_workforce_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_incidents",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("domain", sa.String(length=16), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("declared_blast_radius", sa.Text(), nullable=False),
        sa.Column("derived_blast_radius", sa.JSON(), nullable=False),
        sa.Column("timeline", sa.JSON(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("hypotheses", sa.JSON(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("root_cause_evidence_refs", sa.JSON(), nullable=False),
        sa.Column("root_cause_ref", sa.String(length=64), nullable=True),
        sa.Column("corrective_actions", sa.JSON(), nullable=False),
        sa.Column("regression_test_ids", sa.JSON(), nullable=False),
        sa.Column("eval_case_refs", sa.JSON(), nullable=False),
        sa.Column("follow_ups", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("resolved_at", aeos.core.db.UTCDateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.Column("updated_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ops_incidents", schema=None) as batch_op:
        batch_op.create_index("ix_ops_incidents_status", ["status"], unique=False)

    op.create_table(
        "ops_retirements",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("checklist_hash", sa.String(length=80), nullable=True),
        sa.Column("decision_id", sa.String(length=36), nullable=True),
        sa.Column("legal_hold", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.Column("updated_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("project_id"),
    )

    op.create_table(
        "ops_sli_samples",
        sa.Column("at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.Column("sli_key", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ops_sli_samples", schema=None) as batch_op:
        batch_op.create_index("ix_ops_sli_samples_key_at", ["sli_key", "at"], unique=False)

    # ALTER TABLE ... ADD COLUMN (no table rebuild): append-only triggers on audit_events stay intact.
    op.add_column("users", sa.Column("temp_password_expires_at", aeos.core.db.UTCDateTime(), nullable=True))
    op.add_column("users", sa.Column("temp_password_used_at", aeos.core.db.UTCDateTime(), nullable=True))
    op.add_column("audit_events", sa.Column("run_id", sa.String(length=36), nullable=True))
    op.add_column("context_pack_meta", sa.Column("auth_ref", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("context_pack_meta", sa.Column("status", sa.String(length=16), nullable=False,
                                                 server_default="ACTIVE"))


def downgrade() -> None:
    with op.batch_alter_table("context_pack_meta", schema=None) as batch_op:
        batch_op.drop_column("status")
        batch_op.drop_column("auth_ref")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_column("run_id")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("temp_password_used_at")
        batch_op.drop_column("temp_password_expires_at")
    with op.batch_alter_table("ops_sli_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_ops_sli_samples_key_at")
    op.drop_table("ops_sli_samples")
    op.drop_table("ops_retirements")
    with op.batch_alter_table("ops_incidents", schema=None) as batch_op:
        batch_op.drop_index("ix_ops_incidents_status")
    op.drop_table("ops_incidents")
