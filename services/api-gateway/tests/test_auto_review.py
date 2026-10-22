"""Step 4 (review that learns): suggestions, the auto-reviewer, reopen, Correct mode, the new gates, and the API
the workbench reads (runs, table detail, sources)."""
from pathlib import Path

import httpx
import pytest
from adce_core.artifacts import LocalArtifactStore
from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_core.ports import MemoryKG
from api_gateway.app import Services, create_app
from hitl_service.auto_review import AUTO_REVIEWER, accept_facts, stats
from hitl_service.service import MemoryReviewStore, ReviewService

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "orders_local"
EV = [Evidence(kind="rule_check", ref="t")]


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


async def test_items_carry_suggestions_and_a_decision_can_be_reopened(api):
    c, kg = api
    await upload_orders_local(c)
    await c.post("/apps/orders_local/runs", json={})
    items = (await c.get("/review/items", params={"app_id": "orders_local"})).json()
    assert all("suggestion" in i["detail"] for i in items)
    gate = next(i for i in items if i["kind"] == "approve_sql" and i["subject"].endswith("table:Orders"))
    assert gate["detail"]["suggestion"]["decision"] == "approve"  # V4, nothing failed
    assert any(i["kind"] == "scope" for i in items)  # the files that make up the app are a gate too

    s = gate["detail"]["suggestion"]
    assert (await c.post(f"/review/items/{gate['id']}/decision",
                         json={"decision": s["decision"], "payload": s["payload"]})).status_code == 200
    rep = await kg.facts(kind="report_sql", subject="app:orders_local/table:Orders")
    assert rep[-1].trust == Trust.CONFIRMED
    r = await c.post(f"/review/items/{gate['id']}/reopen")
    assert r.status_code == 200 and r.json()["status"] == "open"
    assert not [f for f in await kg.facts(kind="report_sql", subject="app:orders_local/table:Orders")
                if f.trust == Trust.CONFIRMED]  # the approval is invalidated
    assert (await c.post(f"/review/items/{gate['id']}/reopen")).status_code == 409  # open, not decided


async def test_runs_table_detail_and_sources(api):
    c, _ = api
    await upload_orders_local(c)
    await c.post("/apps/orders_local/runs", json={})
    runs = (await c.get("/apps/orders_local/runs")).json()
    assert len(runs) == 1 and runs[0]["status"] in ("complete", "waiting_for_dremio") and runs[0]["steps"] > 5

    t = (await c.get("/apps/orders_local/tables/Orders")).json()
    assert t["sql"].startswith("WITH") and t["validation"]["level"] == "V4"
    assert t["steps"] and all(st["script"] for st in t["steps"] if st["spans"])
    assert (await c.get("/apps/orders_local/tables/Nope")).status_code == 404

    sources = (await c.get("/apps/orders_local/sources")).json()
    unmapped = [x["source"] for x in sources if not x["dataset"]]
    assert unmapped
    r = await c.post("/review/apps/orders_local/sources", json={"mappings": {unmapped[0]: "lake.sales.t"}})
    assert r.status_code == 200 and unmapped[0] in r.json()["mapped"]
    after = {x["source"]: x["dataset"] for x in (await c.get("/apps/orders_local/sources")).json()}
    assert after[unmapped[0]] == "lake.sales.t"


async def test_correct_sql_of_a_translated_statement(api):
    c, kg = api
    await c.post("/apps/cor/files", files=[("files", ("s.qvs", b"S: LOAD a INLINE [a\n1\n2];\n"
                                                                b"T: LOAD a, a * 2 AS b RESIDENT S;\n", "text/plain"))])
    await c.post("/apps/cor/runs", json={})
    t = (await c.get("/apps/cor/tables/T")).json()
    step = next(st for st in t["steps"] if st["kind"] == "load")
    bad = await c.post("/review/apps/cor/statement_sql",
                       json={"table": "T", "statement": step["statements"][0], "span": step["spans"][0], "sql": "drop"})
    assert bad.status_code == 409
    ok = await c.post("/review/apps/cor/statement_sql",
                      json={"table": "T", "statement": step["statements"][0], "span": step["spans"][0],
                            "sql": 'SELECT "a", "a" * 3 AS "b" FROM src'})
    assert ok.status_code == 200
    await c.post("/apps/cor/runs", json={})
    t2 = (await c.get("/apps/cor/tables/T")).json()
    assert '* 3' in t2["sql"]  # the next run uses the reviewer's SQL instead of the rules'


async def test_auto_reviewer_accepts_only_safe_kinds_and_never_confirms():
    kg = MemoryKG()
    mem = {"statement": 1, "status": "validated"}
    safe = await kg.propose_fact(Fact(kind="translation_memory", subject="app:a/table:T", value={"sql": "x"} | mem,
                                      trust=Trust.VALIDATED, confidence=0.95, impact=1, agent="t", evidence=EV))
    unsure = await kg.propose_fact(Fact(kind="translation_memory", subject="app:a/table:U", value={"sql": "y"} | mem,
                                        trust=Trust.VALIDATED, confidence=0.5, agent="t", evidence=EV))
    await kg.propose_fact(Fact(kind="report_sql", subject="app:a/table:T", value={"level": "V4", "venue": "local"},
                               trust=Trust.VALIDATED, confidence=1.0, agent="t", evidence=EV))
    assert await accept_facts(kg, "a") == 1
    assert await accept_facts(kg, "a") == 0  # once
    acc = await kg.facts(kind="auto_accepted")
    assert [a.value["fact_id"] for a in acc] == [safe] and acc[0].agent == AUTO_REVIEWER
    assert all(f.status == FactStatus.PROPOSED for f in await kg.facts(kind="translation_memory"))
    assert unsure not in {a.value["fact_id"] for a in acc}

    assert (await stats(kg))["reversed"] == 0
    await kg.decide(safe, FactStatus.REJECTED, "owner")
    st = await stats(kg)
    assert st["reversed"] == 1 and st["reversal_rate"] == 1.0


async def test_informational_issues_are_acknowledged_and_blocked_sql_waits():
    kg = MemoryKG()
    await kg.propose_fact(Fact(kind="bi_issue", subject="app:a", value={"code": "loop_unrolled_from_log",
                                                                         "message": "unrolled", "statement": 3},
                               trust=Trust.DOCUMENTED, agent="t", evidence=EV))
    await kg.propose_fact(Fact(kind="report_sql", subject="app:a/table:T",
                               value={"level": "V2", "venue": "local", "blocked_by": ["vMax"]},
                               trust=Trust.VALIDATED, agent="t", evidence=EV))
    review = ReviewService(MemoryReviewStore(), kg)
    await review.refresh("a")
    assert not [i for i in review.queue("a") if i.kind == "approve_sql"]  # the owner's answer comes first
    auto = [i for i in review.queue("a", "decided") if i.decided_by == AUTO_REVIEWER]
    assert [i.kind for i in auto] == ["issue:loop_unrolled_from_log"]
    reopened = await review.reopen(auto[0].id, "owner")
    assert reopened.status == "open"


async def test_correct_mode_supersedes_a_fact():
    kg = MemoryKG()
    fid = await kg.propose_fact(Fact(kind="primary_key", subject="app:a/table:T", value={"columns": ["a"]},
                                     trust=Trust.INFERRED, agent="t", evidence=EV))
    review = ReviewService(MemoryReviewStore(), kg)
    new = await review.correct(fid, {"columns": ["a", "b"]}, "owner", "composite key")
    cur = await kg.facts(kind="primary_key", subject="app:a/table:T")
    assert [f.id for f in cur] == [new] and cur[0].value["columns"] == ["a", "b"] and cur[0].trust == Trust.CONFIRMED
