"""Proposals/research gap closure (master §39-§41, §47, §52).

* ``change_proposals``: ``owner_plan`` (owner-declared acceptance, replacement/cutover/rollback plan,
  REMOVE closure, canary), ``links`` (publication/release/eval-run/observation/ADR records),
  ``trial`` (trial launched under the TRIAL decision) and ``checkpoint_review`` (Claude evidence
  checkpoint bound to the candidate hash).
* ``research_entries``: ``distribution``, ``terms_checked_at`` and ``reviewed_at`` (dated manual
  review of non-GitHub sources).

Hand-written (batch mode for SQLite). Existing rows get empty JSON objects / strings.

Revision ID: 0002_c2_proposals_research
Revises: 0001_initial
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import aeos.core.db

revision = "0002_c2_proposals_research"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_PROPOSAL_JSON = ("owner_plan", "links", "trial", "checkpoint_review")


def upgrade() -> None:
    with op.batch_alter_table("change_proposals", schema=None) as batch_op:
        for name in _PROPOSAL_JSON:
            batch_op.add_column(sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    with op.batch_alter_table("research_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("distribution", sa.String(length=256), nullable=False,
                                      server_default=sa.text("''")))
        batch_op.add_column(sa.Column("terms_checked_at", aeos.core.db.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", aeos.core.db.UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("research_entries", schema=None) as batch_op:
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("terms_checked_at")
        batch_op.drop_column("distribution")
    with op.batch_alter_table("change_proposals", schema=None) as batch_op:
        for name in reversed(_PROPOSAL_JSON):
            batch_op.drop_column(name)
