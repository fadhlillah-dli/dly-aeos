"""Index on ``audit_events (action, at)`` (owner decision C, ADR-0041 amendment 2026-10-26).

The OpenRouter free-model limits count every feasibility probe that reached OpenRouter by its audit event (action
``broker.route_feasibility.probe``, ``upstream: SENT``) in the last minute and the current UTC day - inside the broker's
count lock. Without this index the lookup reads every audit row of the route across its whole history. No data is
changed (the audit trail stays append-only).

Revision ID: 0010_dc_audit_action_time
Revises: 0009_dc_broker_request_created
Create Date: 2026-10-27 09:00:00
"""
from __future__ import annotations

from alembic import op

revision = "0010_dc_audit_action_time"
down_revision = "0009_dc_broker_request_created"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_audit_action_at", "audit_events", ["action", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_action_at", table_name="audit_events")
