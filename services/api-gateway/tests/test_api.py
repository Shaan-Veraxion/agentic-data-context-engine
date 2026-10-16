"""API gateway: upload -> run -> read tables and facts, with MemoryKG and a local artifact store."""
from pathlib import Path

import httpx
import pytest
from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG
from adce_core.settings import Settings, get_settings
from api_gateway.app import Services, create_app
from api_gateway.auth import Principal
from hitl_service.service import MemoryReviewStore, ReviewService

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"


@pytest.fixture
async def client(tmp_path):
    kg = MemoryKG()
    app = create_app(Services(kg=kg, artifacts=LocalArtifactStore(tmp_path),
                              review=ReviewService(MemoryReviewStore(), kg)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def test_upload_run_and_read(client):
    c, _ = client
    files = [("files", (n, (APP / n).read_bytes(), "text/plain")) for n in ("script.qvs", "reload.log")]
    up = await c.post("/apps/sales_overview/files", files=files)
    assert up.status_code == 200 and len(up.json()["files"]) == 2

    run = await c.post("/apps/sales_overview/runs", json={"app_name": "Sales Overview"})
    body = run.json()
    assert run.status_code == 200 and body["status"] == "waiting_for_dremio"
    assert body["tables"] == ["Customers", "RegionMap", "Orders"] and len(body["pending"]) == 2

    summary = await c.get(f"/apps/sales_overview/runs/{body['run_id']}")
    assert summary.status_code == 200 and summary.json()["run_id"] == body["run_id"]

    tables = {t["key"].split(":")[-1]: t for t in (await c.get("/apps/sales_overview/tables")).json()}
    assert tables["Orders"]["trust"] == "documented"
    assert sorted(r["lines_fetched"] for r in tables["Orders"]["loaded_rows"]) == [3120, 48213]

    facts = (await c.get("/facts", params={"kind": "bi_coverage"})).json()
    assert facts[0]["value"]["ratio"] == 1.0


async def test_validation_and_missing(client):
    c, _ = client
    assert (await c.post("/apps/bad id!/runs", json={})).status_code == 422
    assert (await c.post("/apps/empty/runs", json={})).status_code == 409
    assert (await c.get("/apps/x/runs/nope")).status_code == 404


async def test_roles_are_enforced_when_auth_is_on(client):
    c, app = client
    from api_gateway import auth

    app.dependency_overrides[get_settings] = lambda: Settings(auth_mode="keycloak")
    assert (await c.get("/facts")).status_code == 401
    app.dependency_overrides[auth.current_principal] = lambda: Principal(subject="v", roles={"viewer"})
    assert (await c.get("/facts")).status_code == 200
    assert (await c.post("/apps/a/runs", json={})).status_code == 403
    app.dependency_overrides.clear()


async def test_runs_are_queued_for_the_orchestrator_worker(tmp_path):
    from orchestrator.pipeline import Deps
    from orchestrator.worker import MemoryRunQueue, Worker

    kg, store, queue = MemoryKG(), LocalArtifactStore(tmp_path), MemoryRunQueue()
    review = ReviewService(MemoryReviewStore(), kg)
    app = create_app(Services(kg=kg, artifacts=store, review=review, queue=queue))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            files = [("files", (n, (APP / n).read_bytes(), "text/plain")) for n in ("script.qvs", "reload.log")]
            await c.post("/apps/sales_overview/files", files=files)
            run = (await c.post("/apps/sales_overview/runs", json={})).json()
            assert run["status"] == "queued"
            assert (await c.get(f"/apps/sales_overview/runs/{run['run_id']}")).json()["status"] == "queued"
            done = await Worker(queue, Deps(kg=kg, artifacts=store), review=review).drain()
            assert [r.status for r in done] == ["done"] and done[0].result["status"] == "waiting_for_dremio"
            summary = (await c.get(f"/apps/sales_overview/runs/{run['run_id']}")).json()
            assert summary["status"] == "waiting_for_dremio" and summary["decisions"][-1]["action"] == "publish"


async def test_a_run_paused_for_owner_questions_continues_with_the_answers(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver

    kg, store = MemoryKG(), LocalArtifactStore(tmp_path)
    review = ReviewService(MemoryReviewStore(), kg)
    app = create_app(Services(kg=kg, artifacts=store, review=review, checkpointer=InMemorySaver(), interactive=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            files = [("files", ("script.qvs", (APP / "script.qvs").read_bytes(), "text/plain"))]  # no reload log
            await c.post("/apps/sales_overview/files", files=files)
            run = (await c.post("/apps/sales_overview/runs", json={})).json()
            assert run["status"] == "waiting_for_owner" and any("vReloadDate" in q for q in run["questions"])
            items = (await c.get("/review/items", params={"app_id": "sales_overview"})).json()
            item = next(i for i in items if i["kind"] == "unresolved_variable" and "vReloadDate" in i["title"])
            done = await c.post(f"/apps/sales_overview/runs/{run['run_id']}/answers",
                                json={"answers": {item["id"]: "2026-10-16"}})
            body = done.json()
            assert done.status_code == 200 and body["status"] == "waiting_for_dremio"
            assert body["decisions"][-1]["action"] == "publish"
            facts = await kg.facts(kind="variable_value", subject="app:sales_overview/var:vReloadDate")
            assert any(f.trust.value == "confirmed" for f in facts)
