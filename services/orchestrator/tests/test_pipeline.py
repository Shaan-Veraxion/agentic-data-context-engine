"""The qlik_pipeline graph on the fixture app, with the real Postgres checkpointer."""
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from adce_core.artifacts import LocalArtifactStore, get_json
from adce_core.ports import MemoryKG
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from orchestrator.pipeline import Deps, run_pipeline

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"
DSN = os.environ.get("ADCE_TEST_PG_DSN", "postgresql://adce:adce@localhost:5433/adce_test")


def _pg_up() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _upload(store: LocalArtifactStore) -> list[dict]:
    files = []
    for name in ("script.qvs", "reload.log"):
        files.append({"name": name, "key": store.put(f"uploads/sales_overview/{name}", (APP / name).read_bytes())})
    return files


async def test_pipeline_without_checkpointer(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    out: dict = dict(await run_pipeline(app_id="sales_overview", run_id="r1", files=_upload(store),
                                         deps=Deps(kg=kg, artifacts=store)))
    assert out["status"] == "waiting_for_dremio"
    assert out["events"] == ["upload.classified[bi_app]", "bi_ir.ready", "bi_transform.ready", "sql.candidates",
                             "sql.checked", "report_sql.validated", "pack.published"]
    assert sorted(p["source"] for p in out["pending"]) == ["source:Dremio_Sales:sales.crm.customers",
                                                           "source:Dremio_Sales:sales.erp.orders_v2"]
    summary = get_json(store, out["metrics"]["summary_key"])
    assert summary["tables"] == ["Customers", "RegionMap", "Orders"]


async def test_pipeline_stops_cleanly_without_scripts(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    files = [{"name": "reload.log", "key": store.put("u/reload.log", (APP / "reload.log").read_bytes())}]
    out: dict = dict(await run_pipeline(app_id="x", run_id="r1", files=files, deps=Deps(kg=kg, artifacts=store)))
    assert out["status"] == "needs_inputs" and "bi_ir_key" not in out


@pytest.mark.integration
@pytest.mark.skipif(not _pg_up(), reason="local compose stack is not running")
async def test_pipeline_is_checkpointed_in_postgres(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    run_id = uuid.uuid4().hex[:8]
    async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
        await saver.setup()
        out: dict = dict(await run_pipeline(app_id="sales_overview", run_id=run_id, files=_upload(store),
                                             deps=Deps(kg=kg, artifacts=store), checkpointer=saver))
        assert out["status"] == "waiting_for_dremio"
        cfg: RunnableConfig = {"configurable": {"thread_id": f"sales_overview:{run_id}"}}
        saved = await saver.aget_tuple(cfg)
        assert saved is not None and saved.checkpoint["channel_values"]["status"] == "waiting_for_dremio"
        history = [c async for c in saver.alist(cfg)]
        assert len(history) >= 10  # one checkpoint per step: input + 9 nodes
