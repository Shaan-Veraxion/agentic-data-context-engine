"""The agentic qlik_pipeline: the translate subgraph, per-table fan-out, repair, the supervisor, owner questions
(interrupt and resume), re-entry at a later node, and the run budget."""
from pathlib import Path
from typing import Any

from adce_core.agent import AgentContext
from adce_core.artifacts import LocalArtifactStore, get_json
from adce_core.ports import MemoryKG
from agents_reversesql.agents import QueryChecker
from langgraph.checkpoint.memory import InMemorySaver
from orchestrator.budget import RunGateway
from orchestrator.events import start_for
from orchestrator.pipeline import Deps, resume_pipeline, run_pipeline
from orchestrator.supervisor import NextAction, by_llm, options

ROOT = Path(__file__).resolve().parents[3]
APPS = ROOT / "eval" / "gold" / "bi" / "apps"


def _upload(store: LocalArtifactStore, app: str, names: list[str] | None = None) -> list[dict]:
    d = APPS / app
    every = [d / "script.qvs", d / "reload.log", *sorted((d / "qvd").glob("*.qvd")),
             *sorted((d / "sources").rglob("*.csv"))]
    paths = [d / n for n in names] if names else every
    files = []
    for p in paths:
        if not p.exists():
            continue
        name = p.relative_to(d).as_posix() if "sources" in p.parts else p.name
        files.append({"name": name, "key": store.put(f"uploads/{app}/{name}", p.read_bytes())})
    return files


def _nodes(out: dict) -> list[str]:
    return [e["node"] for e in out["trace"]]


async def test_the_run_timeline_shows_every_node_and_the_supervisor_decides_to_publish(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    out: dict = dict(await run_pipeline(app_id="orders_local", run_id="r1", files=_upload(store, "orders_local"),
                                         deps=Deps(kg=kg, artifacts=store)))
    nodes = _nodes(out)
    assert nodes[:4] == ["enter", "intake", "extract", "resolve"]
    assert "translate.prepare" in nodes and "translate.finish" in nodes
    # one check and one reconcile per table (fan-out), then the supervisor
    assert sorted(e["table"] for e in out["trace"] if e["node"] == "check") == ["CustomerTotals", "Orders"]
    assert sorted(e["table"] for e in out["trace"] if e["node"] == "reconcile") == ["CustomerTotals", "Orders"]
    assert out["decisions"] == [{"step": 1, "action": "publish", "table": None, "reason": "the only option",
                                 "by": "rules", "options": ["publish"]}]
    assert nodes[-3:] == ["record", "publish", "summarize"]
    assert out["levels"] == {"Orders": "V4", "CustomerTotals": "V4"}
    summary = get_json(store, out["metrics"]["summary_key"])
    assert summary["decisions"] == out["decisions"] and summary["trace"]


async def test_the_supervisor_sends_a_contradicted_table_to_diagnosis_and_the_fix_goes_round_the_loop(tmp_path):
    """rulefix_local with replayed answers: reconcile finds Products at V3 (values differ), the supervisor picks
    diagnose, agent 26 corrects the statement, and synthesize, check and reconcile run again before publishing."""
    from adce_gateway.config import load_config
    from adce_gateway.gateway import ModelGateway, Recordings

    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    gw = ModelGateway("replay", Recordings(ROOT / "eval" / "recordings"),
                      config=load_config(ROOT / "deploy" / "gateway" / "groq-free.toml"))
    out: dict = dict(await run_pipeline(app_id="rulefix_local", run_id="r1", files=_upload(store, "rulefix_local"),
                                         deps=Deps(kg=kg, artifacts=store, gateway=gw)))
    first = [e for e in out["trace"] if e["node"] == "reconcile"][0]
    assert (first["level"], first["mismatch"]) == ("V3", "values")
    assert [(d["action"], d["table"], d["by"]) for d in out["decisions"]] == [("diagnose", "Products", "rules"),
                                                                               ("publish", None, "rules")]
    diag = next(e for e in out["trace"] if e["node"] == "diagnose")
    assert diag["status"] == "ai_repaired"
    nodes = _nodes(out)
    assert nodes.count("synthesize") == 2 and nodes.count("reconcile") == 2
    assert out["levels"] == {"Products": "V4"}
    pack_sql = store.get(f"{out['pack_prefix']}/sql/Products.sql").decode()
    tr = get_json(store, out["translation_key"])
    assert any(o["status"] == "ai_repaired" for o in tr["ai_outcomes"])
    assert next(iter(tr["overrides"].values())).split()[0].upper() == "SELECT" and "WITH" in pack_sql


async def test_owner_questions_pause_the_run_and_answers_resume_it_from_extract(tmp_path):
    from hitl_service.service import MemoryReviewStore, ReviewService

    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    saver = InMemorySaver()
    deps = Deps(kg=kg, artifacts=store, interactive=True)
    files = _upload(store, "sales_overview", ["script.qvs"])  # no reload log: vReloadDate stays unresolved
    out: dict = dict(await run_pipeline(app_id="sales_overview", run_id="r1", files=files, deps=deps,
                                         checkpointer=saver))
    assert "__interrupt__" in out
    asked = out["__interrupt__"][0].value
    assert any("vReloadDate" in q for q in asked["questions"])
    assert out["decisions"][-1]["action"] == "ask_owner"

    review = ReviewService(MemoryReviewStore(), kg)
    await review.refresh("sales_overview")
    item = next(i for i in review.queue("sales_overview") if i.kind == "unresolved_variable"
                and "vReloadDate" in i.title)
    await review.decide(item.id, "answer", "owner", {"value": "2026-10-16"})
    done: dict = dict(await resume_pipeline(app_id="sales_overview", run_id="r1", deps=deps, checkpointer=saver,
                                            answer={"answered": [item.id]}))
    nodes = _nodes(done)
    assert "ask_owner" in nodes and nodes.count("extract") == 2  # read the script again with the answer
    assert done["status"] == "waiting_for_dremio" and "__interrupt__" not in done
    assert done["decisions"][-1]["action"] == "publish"


async def test_a_rerun_enters_at_translate_with_the_previous_runs_results(tmp_path):
    store, kg = LocalArtifactStore(tmp_path), MemoryKG()
    files = _upload(store, "orders_local")
    first: dict = dict(await run_pipeline(app_id="orders_local", run_id="r1", files=files,
                                           deps=Deps(kg=kg, artifacts=store)))
    from orchestrator.learning import latest_summary

    previous = latest_summary(store, "orders_local")
    assert previous is not None and previous["run_id"] == "r1"
    again: dict = dict(await run_pipeline(app_id="orders_local", run_id="r2", files=files,
                                           deps=Deps(kg=kg, artifacts=store), start_at="translate",
                                           previous=previous))
    nodes = _nodes(again)
    assert nodes[:2] == ["enter", "translate.prepare"] and "intake" not in nodes and "extract" not in nodes
    assert again["levels"] == first["levels"]
    mapped: dict = dict(await run_pipeline(app_id="orders_local", run_id="r3", files=files,
                                            deps=Deps(kg=kg, artifacts=store), start_at="resolve", previous=previous,
                                            reuse_translation=True))
    assert _nodes(mapped)[:3] == ["enter", "resolve", "synthesize"]


def test_events_choose_the_earliest_node_they_need():
    assert start_for(["source mapped: source:x"]) == ("resolve", True)
    assert start_for(["reviewer SQL", "source mapped: source:x"]) == ("resolve", False)
    assert start_for(["variable value", "reviewer SQL"]) == ("extract", False)
    assert start_for(["owner answered questions about a gap"]) == ("translate", False)


class _Answers:
    """A gateway that answers one route with fixed outputs, counting calls."""

    mode = "live"

    def __init__(self, route: str, outputs: list[Any]) -> None:
        self.route, self.outputs, self.calls = route, list(outputs), 0

    async def call(self, route: str, inputs: dict, output_model: Any, **kw: Any) -> Any:
        assert route == self.route
        self.calls += 1

        class R:
            output = self.outputs.pop(0)
            model, cost_usd, tool_calls = "fake", 0.01, []

        return R()


async def test_the_llm_supervisor_picks_an_option_and_falls_back_to_the_rules_outside_them():
    v = {"A": {"level": "V3", "mismatch": {"category": "values", "route": "diagnose", "detail": "A.x.sum"},
               "failed": [{}]},
         "B": {"level": "V4", "mismatch": {"category": "none", "route": "none", "detail": ""}, "failed": []}}
    opts = options(v, {}, has_model=True, can_ask=False)
    assert [(o["action"], o["table"]) for o in opts] == [("diagnose", "A"), ("publish", None)]
    gw = _Answers("plan_next", [NextAction(action="publish", reason="nothing worth fixing")])
    c = await by_llm(gw, {}, opts, task="t")
    assert (c["action"], c["by"]) == ("publish", "llm:fake")
    gw = _Answers("plan_next", [NextAction(action="diagnose", table="B")])
    c = await by_llm(gw, {}, opts, task="t")
    assert (c["action"], c["table"], c["by"]) == ("diagnose", "A", "rules") and "not an option" in c["fallback"]


async def test_agent_25_repairs_a_table_that_fails_a_blocking_rule(tmp_path):
    from adce_qlik.document import read_script
    from adce_qlik.model import build_app
    from adce_reversesql.ai_loop import table_statements
    from adce_reversesql.runtime import SqlRepair, load_translation, save_translation

    script = "T:\nLOAD A, B AS C;\nSQL SELECT A FROM db.sales.t;\n"
    doc = read_script("rep", {"script.qvs": script})
    app = build_app(doc)
    store = LocalArtifactStore(tmp_path)
    from adce_core.artifacts import put_json

    bi = put_json(store, "apps/rep/bi_ir/x.json", {"document": doc.model_dump(mode="json"),
                                                   "app": app.model_dump(mode="json")})
    tk = save_translation(store, "rep", bi, {}, {}, [])
    loaded = load_translation(store, bi, tk)
    stmt = next(s for s in table_statements(loaded.app, "T", {}) if "B" in s["text"])
    fix = SqlRepair(statement=stmt["statement"], sql='SELECT "src"."A" AS "A", CAST(NULL AS VARCHAR) AS "C" FROM src')
    gw = _Answers("repair_sql", [fix])
    ctx = AgentContext(kg=MemoryKG(), artifacts=store, run_id="r", app_id="rep", gateway=gw)
    r = await QueryChecker()({"bi_ir_key": bi, "translation_key": tk}, ctx)
    assert [o["status"] for o in r.outputs["repairs"]] == ["sql_repaired"] and gw.calls == 1
    assert r.outputs["checks"]["T"]["ok"] and r.metrics["passed"] == 1
    tr = get_json(store, r.outputs["translation_key"])
    assert tr["overrides"] == {str(stmt["statement"]): fix.sql}


async def test_the_run_budget_stops_model_calls():
    from adce_gateway.gateway import BudgetExceeded

    gw = RunGateway(_Answers("plan_next", [NextAction(action="publish")] * 3), max_calls=2)
    await gw.call("plan_next", {}, NextAction)
    await gw.call("plan_next", {}, NextAction)
    try:
        await gw.call("plan_next", {}, NextAction)
        raise AssertionError("the third call should be refused")
    except BudgetExceeded:
        pass
    assert gw.left()["calls"] == 2 and gw.mode == "live"
