"""Research entry attachments (wave 7, cluster I2): ``research_entries.attachment_ids``.

§01 U-11 "every intake surface supports file upload, including .md": a manual research entry keeps the ids of the
intake attachments submitted with it (validated: same domain as the entry, not REJECTED by intake scanning).
Existing rows get an empty list.

Revision ID: 0008_w7i2_research_attachments
Revises: 0007_w7c1_broker_reasoning
Create Date: 2026-10-26 03:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_w7i2_research_attachments"
down_revision = "0007_w7c1_broker_reasoning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_entries", sa.Column("attachment_ids", sa.JSON(), nullable=False,
                                                server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("research_entries", schema=None) as batch_op:
        batch_op.drop_column("attachment_ids")
