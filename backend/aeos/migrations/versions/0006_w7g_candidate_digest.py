"""ChangeProposal candidate digest (wave 7, cluster G: pins and supply chain).

* ``change_proposals.candidate_digest`` - §25 tool admission "pinned version/digest": the structured
  ``sha256:<hex>`` of the candidate executable/artifact next to the free-text ``candidate_pin``. Required at
  TRIAL_APPROVE/PROMOTE when the changed component's baseline is digest-pinned in the reviewed
  install-manifest, and compared at ACCEPTED with the installation observation of the proposal's domain.

Revision ID: 0006_w7g_candidate_digest
Revises: 0005_c8_platform_misc
Create Date: 2026-10-25 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_w7g_candidate_digest"
down_revision = "0005_c8_platform_misc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TABLE ... ADD COLUMN (no table rebuild)
    op.add_column("change_proposals", sa.Column("candidate_digest", sa.String(length=80), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("change_proposals", schema=None) as batch_op:
        batch_op.drop_column("candidate_digest")
