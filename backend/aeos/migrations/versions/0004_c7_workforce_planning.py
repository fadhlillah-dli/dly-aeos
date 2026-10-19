"""Workforce/planning gap closure (cluster C7): role revision on TaskSpec, milestone governance fields,
pinned base commit per task execution, current-action source, RoleSkillPack inputs/scope/example outputs.

All columns are additive. JSON list/dict columns get a server default so existing rows stay valid; append-only
triggers are re-installed by aeos.core.migrate after the upgrade.

Revision ID: 0004_c7_workforce_planning
Revises: 0003_c5_eval_execution
Create Date: 2026-10-19 09:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_c7_workforce_planning"
down_revision = "0003_c5_eval_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_specs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role_revision_hash", sa.String(length=80), nullable=True))

    with op.batch_alter_table("milestones", schema=None) as batch_op:
        batch_op.add_column(sa.Column("priority", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("acceptance", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch_op.add_column(sa.Column("depends_on", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch_op.add_column(sa.Column("owner", sa.String(length=128), nullable=True))

    with op.batch_alter_table("task_executions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("base_commit", sa.String(length=64), nullable=True))

    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_action_source", sa.String(length=16), nullable=True))

    with op.batch_alter_table("role_skill_packs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("inputs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch_op.add_column(sa.Column("allowed_scope", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column("example_outputs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("role_skill_packs", schema=None) as batch_op:
        batch_op.drop_column("example_outputs")
        batch_op.drop_column("allowed_scope")
        batch_op.drop_column("inputs")

    with op.batch_alter_table("worker_runs", schema=None) as batch_op:
        batch_op.drop_column("current_action_source")

    with op.batch_alter_table("task_executions", schema=None) as batch_op:
        batch_op.drop_column("base_commit")

    with op.batch_alter_table("milestones", schema=None) as batch_op:
        batch_op.drop_column("owner")
        batch_op.drop_column("depends_on")
        batch_op.drop_column("acceptance")
        batch_op.drop_column("priority")

    with op.batch_alter_table("task_specs", schema=None) as batch_op:
        batch_op.drop_column("role_revision_hash")
