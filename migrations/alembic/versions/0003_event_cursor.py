"""Event consumer positions in the outbox (ADR-6): the orchestrator's event-driven re-runs.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE event_cursor (
        consumer    text PRIMARY KEY,
        seq         bigint NOT NULL DEFAULT 0,
        updated_at  timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE event_cursor")
