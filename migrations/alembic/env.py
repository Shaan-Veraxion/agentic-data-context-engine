"""Alembic environment for the ADCE Postgres schema (fact log, entities, outbox)."""
from adce_core.settings import get_settings
from alembic import context
from sqlalchemy import create_engine


def _url() -> str:
    url = context.config.attributes.get("dsn") or get_settings().pg_dsn
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
