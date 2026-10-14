"""Review loop through the API: run -> review items -> decisions -> facts -> next run uses them."""
from pathlib import Path

import httpx
import pytest
from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from api_gateway.app import Services, create_app
from hitl_service.service import MemoryReviewStore, ReviewService

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"


@pytest.fixture
async def api(tmp_path):
    kg = MemoryKG()
    app = create_app(Services(kg=kg, artifacts=LocalArtifactStore(tmp_path),
                              review=ReviewService(MemoryReviewStore(), kg)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c, kg


async def upload_orders_local(c: httpx.AsyncClient) -> None:
    paths = [APP / "script.qvs", APP / "reload.log", *sorted((APP / "qvd").glob("*.qvd")),
             *sorted((APP / "sources").rglob("*.csv"))]
    files = [("files", (p.relative_to(APP).as_posix() if "sources" in p.parts else p.name, p.read_bytes(),
                        "application/octet-stream")) for p in paths]
    assert (await c.post("/apps/orders_local/files", files=files)).status_code == 200


async def test_gate_approval_confirms_the_sql(api):
    c, kg = api
    await upload_orders_local(c)
    run = (await c.post("/apps/orders_local/runs", json={})).json()
    assert run["levels"] == {"Orders": "V4", "CustomerTotals": "V4"} and run["new_review_items"] > 0

    items = (await c.get("/review/items", params={"app_id": "orders_local"})).json()
    kinds = {i["kind"] for i in items}
    assert {"approve_sql", "map_source"} <= kinds
    gate = next(i for i in items if i["kind"] == "approve_sql" and i["subject"].endswith("table:Orders"))
    bad = await c.post(f"/review/items/{gate['id']}/decision", json={"decision": "provide_sql"})
    assert bad.status_code == 409
    ok = await c.post(f"/review/items/{gate['id']}/decision", json={"decision": "approve"})
    assert ok.status_code == 200 and ok.json()["status"] == "decided"
    rep = await kg.facts(kind="report_sql", subject="app:orders_local/table:Orders")
    assert rep[-1].trust.value == "confirmed" and rep[-1].value["_decided_by"] == "local-dev"

    seed = next(i for i in items if i["kind"] == "map_source" and "orders_v2" in i["subject"])
    await c.post(f"/review/items/{seed['id']}/decision",
                 json={"decision": "map", "payload": {"dataset": "sales.erp.orders_v2"}})
    run2 = (await c.post("/apps/orders_local/runs", json={})).json()
    assert [p["source"] for p in run2["pending"]] == ["source:Dremio_Sales:sales.crm.customers"]

    ov = (await c.get("/apps/orders_local/overview")).json()
    orders = next(t for t in ov["tables"] if t["name"] == "Orders")
    assert orders["level"] == "V4" and orders["venue"] == "local"
    graph = (await c.get("/apps/orders_local/lineage")).json()
    assert {"source", "target", "kind"} <= set(graph["edges"][0])
    assert any(n["type"] == "qvd" for n in graph["nodes"])


async def test_reviewer_sql_and_variable_answers_feed_the_next_run(api):
    c, kg = api
    script = (b"LET vMax = $(=Max(x));\n"
              b"S: LOAD a INLINE [a\n1\n2];\n"
              b"T: LOAD RangeSum(a, 1) AS r RESIDENT S;\n"
              b"U: LOAD a, '$(vMax)' AS m RESIDENT S;\n")
    await c.post("/apps/rev/files", files=[("files", ("s.qvs", script, "text/plain"))])
    await c.post("/apps/rev/runs", json={})
    items = (await c.get("/review/items", params={"app_id": "rev"})).json()
    untranslated = next(i for i in items if i["kind"] == "untranslated")
    r = await c.post(f"/review/items/{untranslated['id']}/decision",
                     json={"decision": "provide_sql", "payload": {"sql": 'SELECT "a" + 1 AS "r" FROM src'}})
    assert r.status_code == 200
    var = next(i for i in items if i["kind"] == "unresolved_variable")
    r = await c.post(f"/review/items/{var['id']}/decision",
                     json={"decision": "answer", "payload": {"name": "vMax", "value": "42"}})
    assert r.status_code == 200, r.text

    run2 = (await c.post("/apps/rev/runs", json={})).json()
    assert run2["levels"]["T"] == "V2"  # translated now (reviewer SQL) and executes locally; no counts to compare
    tr = await kg.facts(kind="bi_transform", subject="app:rev/table:T")
    assert tr[-1].value["untranslated"] == []
    v = await kg.facts(kind="variable_value", subject="app:rev/var:vMax")
    assert v[-1].trust.value == "confirmed" and v[-1].value["value"] == "42"
