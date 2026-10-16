"""Integration fixtures: a fresh schema in the adce_test database and a unique Graphiti group per test module."""
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from adce_core.settings import Settings

TEST_DSN = os.environ.get("ADCE_TEST_PG_DSN", "postgresql://adce:adce@localhost:5433/adce_test")
ROOT = Path(__file__).resolve().parents[3]


def _stack_up() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            pass
        from neo4j import GraphDatabase

        s = Settings()
        with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)) as d:
            d.verify_connectivity()
    except Exception:
        return False
    return True


STACK = _stack_up()


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in item.keywords and not STACK:
            item.add_marker(pytest.mark.skip(reason="local compose stack is not running"))


def migrate_fresh(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["dsn"] = dsn
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
async def service():
    from adce_kg import KnowledgeService

    migrate_fresh(TEST_DSN)
    svc = await KnowledgeService.from_settings(Settings(embedder="hash", tenant="test"), dsn=TEST_DSN,
                                               group_id=f"test_{uuid.uuid4().hex[:10]}")
    yield svc
    if svc.graph:
        await svc.graph.wipe_group()
    await svc.close()
