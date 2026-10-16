"""Fact provenance and tenant isolation (Technical Design §11.1): tenant and scope, run, model and prompt version,
impact, valid_to and invalidated_at on every fact; entities and outbox events per tenant.

Existing rows belong to tenant 'default'. The log stays append-only (the trigger is unchanged): columns are added
with defaults, nothing is rewritten.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE fact_log
        ADD COLUMN tenant_id      text NOT NULL DEFAULT 'default',
        ADD COLUMN scope_id       text,
        ADD COLUMN run_id         text,
        ADD COLUMN model          text,
        ADD COLUMN prompt_version text,
        ADD COLUMN impact         integer,
        ADD COLUMN valid_to       timestamptz,
        ADD COLUMN invalidated_at timestamptz;
    CREATE INDEX fact_log_tenant_kind ON fact_log (tenant_id, kind);
    CREATE INDEX fact_log_tenant_subject ON fact_log (tenant_id, subject);

    ALTER TABLE entity ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
    ALTER TABLE entity DROP CONSTRAINT entity_pkey;
    ALTER TABLE entity ADD PRIMARY KEY (tenant_id, key);

    ALTER TABLE outbox ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
    CREATE INDEX outbox_tenant_seq ON outbox (tenant_id, seq);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX outbox_tenant_seq;
    ALTER TABLE outbox DROP COLUMN tenant_id;
    ALTER TABLE entity DROP CONSTRAINT entity_pkey;
    ALTER TABLE entity DROP COLUMN tenant_id;
    ALTER TABLE entity ADD PRIMARY KEY (key);
    DROP INDEX fact_log_tenant_subject;
    DROP INDEX fact_log_tenant_kind;
    ALTER TABLE fact_log DROP COLUMN invalidated_at, DROP COLUMN valid_to, DROP COLUMN impact,
        DROP COLUMN prompt_version, DROP COLUMN model, DROP COLUMN run_id, DROP COLUMN scope_id, DROP COLUMN tenant_id;
    """)
