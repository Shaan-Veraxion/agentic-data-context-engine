"""Run requests: the API gateway queues pipeline runs (and resumes of paused runs); orchestrator workers run them.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE run_request (
        id           bigserial PRIMARY KEY,
        kind         text NOT NULL CHECK (kind IN ('run', 'resume')),
        app_id       text NOT NULL,
        run_id       text NOT NULL,
        payload      jsonb NOT NULL,
        status       text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'waiting_for_owner', 'error')),
        result       jsonb NOT NULL DEFAULT '{}'::jsonb,
        requested_at timestamptz NOT NULL DEFAULT now(),
        started_at   timestamptz,
        finished_at  timestamptz
    );
    CREATE INDEX run_request_queued ON run_request (id) WHERE status IN ('queued', 'running');
    CREATE INDEX run_request_run ON run_request (app_id, run_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE run_request")
