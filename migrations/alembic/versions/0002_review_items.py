"""Review items for hitl-service.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE review_item (
        id           text PRIMARY KEY,
        app_id       text NOT NULL,
        mode         text NOT NULL CHECK (mode IN ('ask', 'queue', 'gate', 'seed', 'correct')),
        kind         text NOT NULL,
        subject      text NOT NULL,
        fact_id      text,
        title        text NOT NULL,
        detail       jsonb NOT NULL DEFAULT '{}',
        options      jsonb NOT NULL DEFAULT '[]',
        impact       integer NOT NULL DEFAULT 1,
        uncertainty  double precision NOT NULL DEFAULT 0.5,
        status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'decided', 'superseded')),
        decision     text,
        decided_by   text,
        decided_at   timestamptz,
        payload      jsonb,
        result_fact  text,
        created_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX review_item_app_status ON review_item (app_id, status);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE review_item;")
