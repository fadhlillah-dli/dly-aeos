"""Broker request reasoning record (cluster w7c1, ADR-0041 amendment 2026-10-25).

* ``broker_requests.reasoning_effort`` - the reasoning level the broker sent upstream (the route's
  ``mode_params.reasoning``; ``NOT_SENT`` = provider default). A worker-chosen level is never forwarded.
* ``broker_requests.reasoning_requested`` - the level the worker asked for (dropped, recorded as a known level or
  ``UNRECOGNISED``; worker free text is never stored).

Both are nullable: rows recorded before this revision did not observe the value (UNKNOWN). No data is changed:
the corrected OpenCode Zen / Gemini request shape of untouched seeded CANDIDATE routes is applied by ``aeos seed``
(``aeos.routing.seed.run``, which recomputes their config hash), and the broker refuses a superseded seed endpoint
(``ROUTE_ENDPOINT_SUPERSEDED``) until then, so no activated configuration is ever rewritten by a migration.

Revision ID: 0007_w7c1_broker_reasoning
Revises: 0006_w7g_candidate_digest
Create Date: 2026-10-25 12:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_w7c1_broker_reasoning"
down_revision = "0006_w7g_candidate_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broker_requests", sa.Column("reasoning_effort", sa.String(length=32), nullable=True))
    op.add_column("broker_requests", sa.Column("reasoning_requested", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("broker_requests", schema=None) as batch_op:
        batch_op.drop_column("reasoning_requested")
        batch_op.drop_column("reasoning_effort")
