"""Index on ``audit_events (action, at)`` (owner decision C, ADR-0041 amendment 2026-10-26).

Audit lookups by action and time (e.g. the feasibility probes of a route, action ``broker.route_feasibility.probe``,
in the last minute or UTC day) read only that range instead of every audit row of the route. It was added for the
free-model probe count; probes are now counted as broker request rows (``broker.feasibility``), and the index stays for
those lookups. No data is changed (the audit trail stays append-only).

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
