"""Intake router (1) + BI extractor (27) on the sales_overview fixture, with MemoryKG and a local artifact store."""
from pathlib import Path

import pytest
from adce_core.agent import AgentContext, PolicyViolation
from adce_core.artifacts import LocalArtifactStore, get_json
from adce_core.facts import Evidence, Fact, Trust
from adce_core.ports import MemoryKG
from agents_bi_adapters.bi_extractor import BIExtractor
from agents_ingestion.intake_router import IntakeRouter, classify_file

APP = Path(__file__).resolve().parents[3] / "eval" / "gold" / "bi" / "apps" / "sales_overview"


@pytest.fixture
async def run(tmp_path):
    store = LocalArtifactStore(tmp_path)
    kg = MemoryKG()
    qvd = (APP / "Orders.qvd.header.xml").read_bytes() + b"\r\n\x00" + bytes(64)
    files = []
    inputs = [("script.qvs", (APP / "script.qvs").read_bytes()), ("reload.log", (APP / "reload.log").read_bytes()),
              ("Orders.qvd", qvd), ("Sales Overview.qvf", b"\x00binary")]
    for name, data in inputs:
        files.append({"name": name, "key": store.put(f"uploads/sales_overview/{name}", data)})
    ctx = AgentContext(kg=kg, artifacts=store, run_id="r1", app_id="sales_overview")
    intake = await IntakeRouter()({"app_id": "sales_overview", "app_name": "Sales Overview", "files": files}, ctx)
    extract = await BIExtractor()({"files": intake.outputs["files"]}, ctx)
    return kg, store, intake, extract


def test_classification():
    assert classify_file("a.qvs", b"LOAD 1 AUTOGENERATE 1;") == "script"
    assert classify_file("x.txt", b"20261001T000000.000+0000 Execution started.") == "reload_log"
    assert classify_file("Orders.qvd", b"<?xml?><QvdTableHeader>") == "qvd"
    assert classify_file("App.qvf", b"\x00") == "app_binary"
    assert classify_file("export.xlsx", b"PK") == "report_export"


async def test_intake_classifies_and_records_inputs(run):
    kg, _store, intake, _ = run
    classes = {f["name"]: f["class"] for f in intake.outputs["files"]}
    assert classes == {"script.qvs": "script", "reload.log": "reload_log", "Orders.qvd": "qvd",
                       "Sales Overview.qvf": "app_binary"}
    assert intake.metrics["unreadable"] == ["Sales Overview.qvf"]
    assert len(await kg.facts(kind="bi_input")) == 4
    assert (await kg.entity("app:sales_overview")).name == "Sales Overview"


async def test_extractor_writes_bi_ir_and_documented_facts(run):
    kg, store, _, extract = run
    assert extract.events == ["bi_ir.ready"] and extract.metrics["coverage"] == 1.0
    ir = get_json(store, extract.outputs["bi_ir_key"])
    assert set(ir["app"]["tables"]) == {"Customers", "RegionMap", "Orders"}

    facts = await kg.facts()
    assert all(f.trust == Trust.DOCUMENTED and f.evidence for f in facts)
    orders = "app:sales_overview/table:Orders"
    reads = {(f.object, f.value["via"]) for f in await kg.facts(kind="reads_source", subject=orders)}
    assert reads == {("source:Dremio_Sales:sales.erp.orders_v2", "sql"), ("app:sales_overview/table:Customers",
                                                                         "resident")}
    stores = await kg.facts(kind="stores_qvd", subject=orders)
    assert [f.object for f in stores] == ["qvd:lib://QVD/Orders.qvd"]

    # reload-log counts attributed to the exact statement by line number
    counts = {(f.value["action"], f.value["lines_fetched"]) for f in await kg.facts(kind="loaded_rows",
                                                                                   subject=orders)}
    assert counts == {("create", 48213), ("join", 3120)}

    # QVD header linked to the STORE target, ready for V3 checks
    qh = await kg.facts(kind="qvd_header", subject="qvd:lib://QVD/Orders.qvd")
    assert qh and qh[0].value["records"] == 47902

    var = await kg.facts(kind="variable_value", subject="app:sales_overview/var:vReloadDate")
    assert var[0].value == {"value": "2026-10-01", "origin": "reload_log"}
    assert (await kg.facts(kind="bi_coverage"))[0].value["ratio"] == 1.0

    src = await kg.entity("source:Dremio_Sales:sales.erp.orders_v2")
    assert src is not None and src.attributes["resolution"] == "pending_dremio"


async def test_rerun_is_idempotent(run):
    kg, store, intake, _ = run
    before = len(kg.log)
    ctx = AgentContext(kg=kg, artifacts=store, run_id="r2", app_id="sales_overview")
    await BIExtractor()({"files": intake.outputs["files"]}, ctx)
    assert len(kg.log) == before


async def test_agents_cannot_write_undeclared_fact_kinds(run):
    kg, store, _, _ = run
    ctx = AgentContext(kg=kg, artifacts=store, run_id="r3", app_id="sales_overview")
    with pytest.raises(PolicyViolation):
        await BIExtractor().propose(ctx, Fact(kind="primary_key", subject="x", value={}, trust=Trust.INFERRED,
                                              agent="x", evidence=[Evidence(kind="x", ref="x")]), None)  # type: ignore[arg-type]
