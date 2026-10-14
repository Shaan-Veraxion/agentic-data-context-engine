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
