"""End to end on the local compose stack, nothing mocked:
FastAPI (real lifespan) -> LangGraph (Postgres checkpointer) -> agents -> KnowledgeService (Postgres fact log +
Graphiti on Neo4j) and S3 artifacts on SeaweedFS."""
import os
import uuid
from pathlib import Path

import httpx
import pytest
from adce_core.settings import Settings, get_settings

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"
ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


def _test_db(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/adce_test"


def _stack_up() -> bool:
    try:
        import psycopg
        from adce_core.artifacts import s3_client
        from neo4j import GraphDatabase

        s = Settings()
        with psycopg.connect(_test_db(s.pg_dsn), connect_timeout=2):
            pass
        with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)) as d:
            d.verify_connectivity()
        s3_client(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_region).list_buckets()
    except Exception:
        return False
    return True


@pytest.fixture
async def stack_client(monkeypatch):
    if not _stack_up():
        pytest.skip("local compose stack is not running")
    base = Settings()
    group = f"e2e_{uuid.uuid4().hex[:8]}"
    dsn = _test_db(base.pg_dsn)
    monkeypatch.setenv("ADCE_PG_DSN", dsn)
    monkeypatch.setenv("ADCE_KG_GROUP", group)
    monkeypatch.setenv("ADCE_S3_BUCKET", "adce-e2e")
    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["dsn"] = dsn
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    from api_gateway.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     timeout=120) as c:
            yield c, app
        await app.state.services.kg.graph.wipe_group()
    get_settings.cache_clear()
    os.environ.pop("ADCE_KG_GROUP", None)


async def test_full_stack_run(stack_client):
    c, app = stack_client
    qvd = (APP / "Orders.qvd.header.xml").read_bytes() + b"\r\n\x00" + bytes(64)
    files = [("files", (n, (APP / n).read_bytes(), "text/plain")) for n in ("script.qvs", "reload.log")]
    files.append(("files", ("Orders.qvd", qvd, "application/octet-stream")))
    assert (await c.post("/apps/sales_overview/files", files=files)).status_code == 200

    body = (await c.post("/apps/sales_overview/runs", json={"app_name": "Sales Overview"})).json()
    assert body["status"] == "waiting_for_dremio", body

    tables = {t["key"].split(":")[-1]: t for t in (await c.get("/apps/sales_overview/tables")).json()}
    assert set(tables) == {"Customers", "RegionMap", "Orders"}

    kg = app.state.services.kg
    # the same knowledge is in Graphiti: search finds the Orders source link
    hits = await kg.search("orders_v2")
    assert any(h.kind == "reads_source" for h in hits)
    ctx = await kg.context("app:sales_overview/table:Orders")
    assert {n["kind"] for n in ctx["neighbourhood"]} >= {"reads_source", "stores_qvd"}
    assert kg.log.pending() == []  # every outbox event projected
