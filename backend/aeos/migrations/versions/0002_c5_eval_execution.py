"""Evaluation execution/comparison columns and failure-derived case candidates (cluster C5).

* ``eval_runs``: route binding (``route_config_hash``), baseline flag, proposal link, equal-conditions hash,
  environment fingerprint and case-slot bindings used by the trusted collector (§23, §40, §43, §44).
* ``eval_case_candidates``: CANDIDATE regression/eval cases derived from observed failures (§28); adopted
  only by a human PROMOTION decision into a new protected suite.

Revision ID: 0002_c5_eval_execution
Revises: 0001_initial
Create Date: 2026-10-19 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import aeos.core.db

revision = "0002_c5_eval_execution"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("route_config_hash", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("is_baseline", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("proposal_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("conditions_hash", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("environment", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("bindings", sa.JSON(), nullable=False, server_default="[]"))

    op.create_table(
        "eval_case_candidates",
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=16), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("role_key", sa.String(length=64), nullable=True),
        sa.Column("related_suite_key", sa.String(length=96), nullable=True),
        sa.Column("case", sa.JSON(), nullable=False),
        sa.Column("case_hash", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("proposed_by", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=True),
        sa.Column("adopted_suite_key", sa.String(length=96), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.Column("updated_at", aeos.core.db.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("eval_case_candidates", schema=None) as batch_op:
        batch_op.create_index("ix_eval_case_candidates_source", ["source_type", "source_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("eval_case_candidates", schema=None) as batch_op:
        batch_op.drop_index("ix_eval_case_candidates_source")
    op.drop_table("eval_case_candidates")
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.drop_column("bindings")
        batch_op.drop_column("environment")
        batch_op.drop_column("conditions_hash")
        batch_op.drop_column("proposal_id")
        batch_op.drop_column("is_baseline")
        batch_op.drop_column("route_config_hash")
