"""Index on ``broker_requests.created_at`` (owner decision C, ADR-0041 amendment 2026-10-26).

The free-model limits of OpenRouter (requests per minute and per UTC day, per account) are enforced by counting the
broker requests this instance forwarded to ``:free`` models in the last minute and the current UTC day
(``aeos.routing.free_tier.usage``) - on every proxied request, routing pass and admission. Without an index that
count scans the whole table, which only grows. No data is changed.

Revision ID: 0009_dc_broker_request_created
Revises: 0008_w7i2_research_attachments
Create Date: 2026-10-26 18:00:00
"""
from __future__ import annotations

from alembic import op

revision = "0009_dc_broker_request_created"
down_revision = "0008_w7i2_research_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_broker_requests_created", "broker_requests", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_broker_requests_created", table_name="broker_requests")
