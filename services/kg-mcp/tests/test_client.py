"""McpKG: the knowledge port over MCP gives agents the same results as in-process, with kg-mcp's policy enforced."""
from pathlib import Path

import pytest
from adce_core.artifacts import LocalArtifactStore
from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_core.ports import MemoryKG, PolicyError
from kg_mcp.client import McpKG

ROOT = Path(__file__).resolve().parents[3]


async def test_round_trip_and_policy():
    async with McpKG.serving(MemoryKG()) as kg:
        f = Fact(kind="bi_table", subject="app:a/table:T", value={"fields": ["x"]}, trust=Trust.DOCUMENTED,
                 evidence=[Evidence(kind="script_span", ref="a:1-2")], agent="bi_extractor")
        fid = await kg.propose_fact(f)
        assert fid == f.id  # the same content id on both sides
        got = await kg.facts(kind="bi_table")
        assert [g.id for g in got] == [fid] and got[0].value == {"fields": ["x"]}
        with pytest.raises(PolicyError):
            await kg.propose_fact(f.model_copy(update={"evidence": []}))
        with pytest.raises(PolicyError):
            await kg.propose_fact(f.model_copy(update={"trust": Trust.CONFIRMED}))


async def test_similar_facts_rank_by_word_overlap():
    async with McpKG.serving(MemoryKG()) as kg:
        for i, stmt in enumerate(["LOAD SubField(Path, '/', 3) AS Country", "LOAD Upper(Name) AS N"]):
            await kg.propose_fact(Fact(kind="translation_memory", subject=f"memory:{i}", value={"statement": stmt},
                                       trust=Trust.INFERRED, evidence=[Evidence(kind="rule_check", ref="t")],
                                       agent="bi_script_translator"))
        tools = {t.name: t for t in await kg.tools()}
        hits = await tools["find_similar_facts"].run({"kind": "translation_memory", "field": "statement",
                                                      "text": "LOAD SubField(Code, '-', 2) AS Part"})
        assert hits[0]["value"]["statement"].startswith("LOAD SubField")
        with pytest.raises(PolicyError):
            await kg.tools(("propose_fact",))  # models never get write tools


async def test_pipeline_through_mcp_matches_in_process(tmp_path):
    from orchestrator.pipeline import Deps, run_pipeline

    app = ROOT / "eval" / "gold" / "bi" / "apps" / "orders_local"
    store = LocalArtifactStore(tmp_path)
    files = []
    for p in [app / "script.qvs", app / "reload.log", *sorted((app / "qvd").glob("*.qvd")),
              *sorted((app / "sources").rglob("*.csv"))]:
        name = p.relative_to(app).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/o/{name}", p.read_bytes())})
    inner = MemoryKG()
    async with McpKG.serving(inner) as kg:
        out = dict(await run_pipeline(app_id="orders_local", run_id="r1", files=files,
                                      deps=Deps(kg=kg, artifacts=store)))
        assert kg.calls >= 5  # reads and (batched) writes went through MCP
    assert out["levels"] == {"Orders": "V4", "CustomerTotals": "V4"}
    assert len(await inner.facts(kind="report_sql")) == 2


async def test_feedback_history_and_events_over_mcp():
    mem = MemoryKG()
    proposal = Fact(kind="report_sql", subject="app:a/table:T", value={"level": "V2", "sql_sha256": "x"},
                    trust=Trust.INFERRED, evidence=[Evidence(kind="query_result", ref="r")], agent="reconciler")
    fid = await mem.propose_fact(proposal)
    await mem.decide(fid, FactStatus.REJECTED, "ana", {"reason": "Amount must exclude returns"})
    async with McpKG.serving(mem) as kg:
        [rec] = await kg.feedback_history(app_id="a")
        assert rec["decision"] == "rejected" and rec["reason"] == "Amount must exclude returns"
        assert rec["proposed_by"] == "reconciler" and rec["proposal"]["level"] == "V2"
        tools = {t.name for t in await kg.tools()}
        assert "get_feedback_history" in tools and "subscribe_events" not in tools  # events are the orchestrator's
        events = await kg.events()
        assert [e["topic"] for e in events] == ["fact.proposed", "fact.rejected"]
        assert await kg.events(after=2) == []
