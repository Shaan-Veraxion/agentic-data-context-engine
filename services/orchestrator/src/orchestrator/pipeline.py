"""qlik_pipeline: the starting-stage graph, built with LangGraph and checkpointed in Postgres.

intake (1) -> extract (27) -> resolve -> translate (28) -> synthesize (24) -> check (25) -> reconcile (26)
-> publish (19-lite, 20-lite) -> summarize
resolve records which logical sources still wait for Dremio resolution (phase PD) instead of guessing; until then
reconcile runs in the local venue (DuckDB over QVDs and source extracts) and labels its results 'local'.
Added later: grain (needs Dremio, PD), report parsing and figure-level checks (P3).

Dependencies that cannot be checkpointed (knowledge port, artifact store, gateway) travel in the LangGraph
runtime context; the checkpointed state holds only plain data, so a run resumes after a crash.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from adce_core.agent import AgentContext
from adce_core.artifacts import ArtifactStore, put_json
from adce_core.ports import KGPort
from agents_bi_adapters.bi_extractor import BIExtractor
from agents_bi_adapters.script_translator import ScriptTranslator
from agents_ingestion.intake_router import IntakeRouter
from agents_reversesql.agents import Publisher, QueryChecker, ReconcilerAgent, Synthesizer
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {**a, **b}


class _Required(TypedDict):
    app_id: str
    run_id: str
    files: list[dict[str, Any]]


class PipelineState(_Required, total=False):
    app_name: str
    classified: list[dict[str, Any]]
    bi_ir_key: str
    translation_key: str
    validation_key: str
    figures_key: str
    levels: dict[str, str]
    pack_prefix: str
    tables: list[str]
    sources: list[str]
    pending: list[dict[str, Any]]
    status: str
    events: Annotated[list[str], operator.add]
    metrics: Annotated[dict[str, Any], _merge]


@dataclass
class Deps:
    kg: KGPort
    artifacts: ArtifactStore
    gateway: Any | None = None


def _ctx(state: PipelineState, runtime: Runtime[Deps]) -> AgentContext:
    d = runtime.context
    return AgentContext(kg=d.kg, artifacts=d.artifacts, run_id=state["run_id"], app_id=state["app_id"],
                        gateway=d.gateway)


async def intake(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await IntakeRouter()({"app_id": state["app_id"], "app_name": state.get("app_name"),
                              "files": state["files"]}, _ctx(state, runtime))
    return {"classified": r.outputs["files"], "events": r.events, "metrics": {"intake": r.metrics}}


async def extract(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await BIExtractor()({"files": state.get("classified", [])}, _ctx(state, runtime))
    if "bi_ir_key" not in r.outputs:
        return {"status": "needs_inputs", "metrics": {"extract": r.metrics}}
    return {"bi_ir_key": r.outputs["bi_ir_key"], "tables": r.outputs["tables"], "sources": r.outputs["sources"],
            "events": r.events, "metrics": {"extract": r.metrics}}


async def resolve(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Map logical sources to Dremio datasets. Without Dremio (until phase PD) every SQL source stays pending;
    a reviewer-confirmed mapping (fact kind resolves_to) is honoured when present."""
    kg = runtime.context.kg
    pending = []
    for key in state.get("sources", []):
        subject = f"qvd:{key[1:]}" if key.startswith(":") and key.lower().endswith(".qvd") else f"source:{key}"
        confirmed = [f for f in await kg.facts(kind="resolves_to", subject=subject) if f.trust.value == "confirmed"]
        if not confirmed and not subject.startswith("qvd:"):
            pending.append({"source": subject, "waits_for": "dremio_catalog (phase PD)"})
    return {"pending": pending, "metrics": {"resolve": {"pending": len(pending)}}}


async def translate(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await ScriptTranslator()({"bi_ir_key": state.get("bi_ir_key"), "files": state.get("classified", [])},
                                 _ctx(state, runtime))
    return {"translation_key": r.outputs["translation_key"], "events": r.events, "metrics": {"translate": r.metrics}}


def _job(state: PipelineState) -> dict[str, Any]:
    return {"bi_ir_key": state.get("bi_ir_key"), "translation_key": state.get("translation_key"),
            "files": state.get("classified", []), "validation_key": state.get("validation_key"),
            "figures_key": state.get("figures_key"),
            "inputs": [{"name": f["name"], "sha256": f.get("sha256"), "class": f["class"]}
                       for f in state.get("classified", [])]}


async def synthesize(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await Synthesizer()(_job(state), _ctx(state, runtime))
    return {"events": r.events, "metrics": {"synthesize": {"tables": len(r.outputs["sql_keys"])}}}


async def check(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await QueryChecker()(_job(state), _ctx(state, runtime))
    return {"events": r.events, "metrics": {"check": r.metrics}}


async def reconcile(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await ReconcilerAgent()(_job(state), _ctx(state, runtime))
    return {"validation_key": r.outputs["validation_key"], "figures_key": r.outputs["figures_key"],
            "levels": r.outputs["levels"], "events": r.events,
            "metrics": {"reconcile": r.metrics}}


async def publish(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await Publisher()(_job(state), _ctx(state, runtime))
    return {"pack_prefix": r.outputs["pack_prefix"], "events": r.events, "metrics": {"publish": r.metrics}}


async def summarize(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    summary = {k: state.get(k) for k in ("app_id", "run_id", "bi_ir_key", "translation_key", "tables", "sources",
                                          "pending", "levels", "pack_prefix", "events", "metrics")}
    key = put_json(runtime.context.artifacts, f"apps/{state['app_id']}/runs/{state['run_id']}/summary.json", summary)
    status = "waiting_for_dremio" if state.get("pending") else "complete"
    return {"status": status, "metrics": {"summary_key": key}}


def _after_extract(state: PipelineState) -> str:
    return END if state.get("status") == "needs_inputs" else "resolve"


def build_graph() -> StateGraph[PipelineState, Deps, PipelineState, PipelineState]:
    g = StateGraph(PipelineState, context_schema=Deps)
    g.add_node("intake", intake)
    g.add_node("extract", extract)
    g.add_node("resolve", resolve)
    for name, fn in (("translate", translate), ("synthesize", synthesize), ("check", check),
                     ("reconcile", reconcile), ("publish", publish)):
        g.add_node(name, fn)
    g.add_node("summarize", summarize)
    g.add_edge(START, "intake")
    g.add_edge("intake", "extract")
    g.add_conditional_edges("extract", _after_extract, ["resolve", END])
    g.add_edge("resolve", "translate")
    g.add_edge("translate", "synthesize")
    g.add_edge("synthesize", "check")
    g.add_edge("check", "reconcile")
    g.add_edge("reconcile", "publish")
    g.add_edge("publish", "summarize")
    g.add_edge("summarize", END)
    return g


async def run_pipeline(*, app_id: str, run_id: str, files: list[dict[str, Any]], deps: Deps,
                       checkpointer: Any | None = None, app_name: str | None = None) -> PipelineState:
    graph = build_graph().compile(checkpointer=checkpointer)
    config: RunnableConfig = {"configurable": {"thread_id": f"{app_id}:{run_id}"}}
    state: PipelineState = {"app_id": app_id, "run_id": run_id, "files": files, "events": [], "metrics": {}}
    if app_name:
        state["app_name"] = app_name
    return await graph.ainvoke(state, config=config, context=deps)  # type: ignore[return-value]
