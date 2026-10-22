"""Two applications linked by a QVD, run as one scope (POST /runs): the consumer's Dremio SQL inlines the producer's,
lineage and impact cross the applications, and a producer change re-runs the consumer."""
import httpx
import pytest
from adce_core import queries
from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from api_gateway.app import Services, create_app
from hitl_service.service import MemoryReviewStore, ReviewService

PRODUCER = (b"LIB CONNECT TO 'erp';\n"
            b"Orders: SQL SELECT id, amount FROM erp.orders;\n"
            b"STORE Orders INTO [lib://QVD/Orders.qvd] (qvd);\n")
CONSUMER = (b"Totals: LOAD id, Sum(amount) AS total FROM [lib://Shared/orders.qvd] (qvd) GROUP BY id;\n"
            b"Other: LOAD id FROM [lib://QVD/Missing.qvd] (qvd);\n")


@pytest.fixture
async def api(tmp_path):
    kg = MemoryKG()
    store = LocalArtifactStore(tmp_path)
    app = create_app(Services(kg=kg, artifacts=store, review=ReviewService(MemoryReviewStore(), kg)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c, kg, store


async def test_a_scope_run_inlines_the_producer(api):
    c, kg, store = api
    await c.post("/apps/extract/files", files=[("files", ("extract.qvs", PRODUCER, "text/plain"))])
    await c.post("/apps/report/files", files=[("files", ("report.qvs", CONSUMER, "text/plain"))])
    r = (await c.post("/runs", json={"apps": ["report", "extract"]})).json()
    assert r["order"] == ["extract", "report"]
    # the consumer ran before its producer was known, so it ran again from resolve
    assert [x["app_id"] for x in r["runs"]] == ["report", "extract", "report"] and r["runs"][-1]["again"]

    ov = (await c.get("/apps/report/overview")).json()
    prefix = ov["latest_pack"]["prefix"]
    sql = store.get(f"{prefix}/sql/tables/Totals.sql").decode()
    assert "extract__" in sql and "FROM qvd__orders" in sql and "FROM erp.orders" in sql  # the producer's chain
    assert "lib://Shared/orders.qvd" not in sql
    sections = store.get(f"{prefix}/review/open-items.md").decode().split("## ")
    qvds = next(s for s in sections if s.startswith("QVDs read whose producer"))
    assert "missing.qvd" in qvds and "orders.qvd" not in qvds
    unmapped = next(s for s in sections if s.startswith("Sources without a Dremio dataset"))
    assert "sql:erp.orders" in unmapped  # read through the inlined producer, still to map
    assert "made by application extract" in store.get(f"{prefix}/lineage/lineage.md").decode()

    up = {x["table"] for x in await queries.lineage(kg, "app:report/table:Totals", "upstream")}
    assert "app:extract/table:Orders" in up
    down = {x["table"] for x in await queries.lineage(kg, "app:extract/table:Orders", "downstream")}
    assert "app:report/table:Totals" in down  # an approval of Orders now counts the consumer as impact


async def test_a_producer_change_re_runs_its_consumers(api):
    from orchestrator.events import affected, start_for

    c, kg, _ = api
    await c.post("/apps/extract/files", files=[("files", ("extract.qvs", PRODUCER, "text/plain"))])
    await c.post("/apps/report/files", files=[("files", ("report.qvs", CONSUMER, "text/plain"))])
    await c.post("/runs", json={"apps": ["extract", "report"]})
    events = [e for e in await kg.events() if e["payload"].get("fact", {}).get("kind") == "qvd_produced"]
    assert events
    apps = await affected(kg, events)
    assert list(apps) == ["report"] and apps["report"][0].startswith("producer changed: orders.qvd")
    assert start_for(apps["report"]) == ("resolve", True)
