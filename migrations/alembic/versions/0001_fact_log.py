"""Fact log, entities and outbox.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE entity (
        key         text PRIMARY KEY,
        type        text NOT NULL,
        name        text NOT NULL,
        attributes  jsonb NOT NULL DEFAULT '{}',
        created_at  timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE fact_log (
        seq           bigserial PRIMARY KEY,
        fact_id       text NOT NULL UNIQUE,
        kind          text NOT NULL,
        subject       text NOT NULL,
        object        text,
        value         jsonb NOT NULL,
        trust         text NOT NULL CHECK (trust IN ('confirmed', 'documented', 'validated', 'inferred')),
        confidence    double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        evidence      jsonb NOT NULL,
        agent         text NOT NULL,
        agent_version text NOT NULL,
        status        text NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected', 'superseded')),
        supersedes    text REFERENCES fact_log (fact_id),
        valid_from    timestamptz,
        recorded_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX fact_log_kind ON fact_log (kind);
    CREATE INDEX fact_log_subject ON fact_log (subject);
    CREATE INDEX fact_log_object ON fact_log (object);
    CREATE INDEX fact_log_supersedes ON fact_log (supersedes);

    -- The fact log is append-only: corrections are new facts that supersede old ones.
    CREATE FUNCTION fact_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'fact_log is append-only (% not allowed)', TG_OP;
    END $$;
    CREATE TRIGGER fact_log_no_update BEFORE UPDATE OR DELETE ON fact_log
        FOR EACH ROW EXECUTE FUNCTION fact_log_append_only();

    CREATE TABLE outbox (
        seq           bigserial PRIMARY KEY,
        topic         text NOT NULL,
        payload       jsonb NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        projected_at  timestamptz
    );
    CREATE INDEX outbox_pending ON outbox (seq) WHERE projected_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE outbox;
    DROP TRIGGER fact_log_no_update ON fact_log;
    DROP FUNCTION fact_log_append_only();
    DROP TABLE fact_log;
    DROP TABLE entity;
    """)
