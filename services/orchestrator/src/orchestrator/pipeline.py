"""qlik_pipeline: the starting-stage graph (LangGraph), checkpointed in Postgres.

    enter ─┬─▶ intake (1) ─▶ extract (27) ─▶ resolve ─▶ translate (28, subgraph) ─▶ synthesize (24)
           │      ▲                                                                       │
           │      └──────────── ask_owner (interrupt) ◀──┐                       Send per table
           │                                             │                                ▼
           └─ re-entry at extract / resolve / translate  │            check_one (25) ×N ─▶ check_join
                                                         │                                │ failing rules?
                                                         │                     repair (25) ┤ (once; back to synthesize)
                                                         │                                ▼
                                       diagnose (26) ◀── supervisor ◀── reconcile_one (26) ×N   (Send per table)
                                       back to synthesize │
                                                          ▼
                                                 record (26) ─▶ publish (20) ─▶ summarize

The translate subgraph runs agent 28's steps as nodes, one per step of one statement, so every model attempt is a
checkpointed step on the run timeline:

    prepare ─▶ pick ─▶ memory ─▶ propose ⟲ (retry with the problems found) ─▶ pick ... ─▶ finish

- prepare: rules, reviewer SQL, learned rule corrections (ai_loop.apply_rule_fixes)
- pick: the next root statement still untranslated (rounds: a statement that failed only because an earlier one
  did is picked in the next round)
- memory: an accepted gap, or a learned answer checked on this application's data (ai_loop.from_memory)
- propose: one translate_statement call, judged by code (ai_loop.judge_statement); retried with the problems
- finish: what was learned recorded, gaps explained (explain_gap), translation artifact and facts written

After check, a table failing blocking rules is repaired once by agent 25 (route repair_sql) and the SQL is checked
again. After reconcile, the supervisor (orchestrator.supervisor: the model when it is on, else rules) picks the next
action among those code allows: diagnose a table (agent 26, route diagnose_mismatch; a correction goes back
through synthesize, check and reconcile), ask the owner (LangGraph interrupt(); resumes with the answers and
extracts again), or publish. Loops are bounded: one repair round, one diagnosis per table, at most
Deps.max_steps supervisor decisions, and the run's budget (orchestrator.budget).

Dependencies that cannot be checkpointed (knowledge port, artifact store, gateway, the translation's working
objects) travel in the LangGraph runtime context (Deps); the checkpointed state holds only plain data (artifact
keys, overrides, outcomes, validation entries, decisions, the trace), so a run resumes after a crash or an
interrupt.
"""
from __future__ import annotations

import copy
import operator
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Annotated, Any, TypedDict

from adce_core.agent import AgentContext, AgentResult, BaseAgent
from adce_core.artifacts import ArtifactStore, put_json
from adce_core.ports import KGPort
from adce_reversesql.ai_loop import (
    Outcome,
    apply_rule_fixes,
    call_model,
    from_memory,
    judge_statement,
    next_statements,
    script_settings,
    statement_request,
)
from adce_reversesql.compose import Untranslated
from adce_reversesql.runtime import StatementTranslation, load_translation
from agents_bi_adapters.bi_extractor import BIExtractor
from agents_bi_adapters.script_translator import ScriptTranslator, TranslateWork
from agents_ingestion.intake_router import IntakeRouter
from agents_reversesql.agents import Publisher, QueryChecker, ReconcilerAgent, Synthesizer, output_tables
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt

from .budget import RunGateway
from .supervisor import by_llm, by_rules, options, plan_inputs

MAX_ROUNDS, MAX_ATTEMPTS = 5, 4  # translate: rounds over the untranslated roots; model answers per statement
REPAIR_ROUNDS = 1  # agent 25: repair passes per run (each tries a table at most twice)
ENTRIES = ("intake", "extract", "resolve", "translate")
ACCEPTED = ("memory", "ai_validated", "ai_checked")


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {**a, **b}


class _Required(TypedDict):
    app_id: str
    run_id: str
    files: list[dict[str, Any]]


class PipelineState(_Required, total=False):
    app_name: str
    start_at: str  # where the run enters: intake (default), extract, resolve or translate (re-runs)
    reuse_translation: bool  # a re-run for a source mapping only: synthesize from the previous translation
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
    trace: Annotated[list[dict[str, Any]], operator.add]  # every node visited, in order: the run timeline
    decisions: Annotated[list[dict[str, Any]], operator.add]  # the supervisor's choices, with who decided and why
    tr: dict[str, Any]  # the translate subgraph: overrides, tried, queue, round, outcomes, current, stop, next
    checks: Annotated[dict[str, dict[str, Any]], _merge]  # table -> {ok, failing}
    validations: Annotated[dict[str, dict[str, Any]], _merge]  # table -> the reconciler's entry
    loop: dict[str, Any]  # steps, repairs, repaired, diagnosed {table: n}, fixed, asked, answered
    next: dict[str, Any]  # the supervisor's last choice
    questions: list[str]  # owner questions the supervisor may ask
    table: str  # set on a per-table branch (Send)
    out_tables: list[str]  # the tables the pack covers (agents.output_tables), set by synthesize
    has_model: bool  # the run has a model gateway (repair and diagnosis need one)


@dataclass
class Deps:
    kg: KGPort
    artifacts: ArtifactStore
    gateway: Any | None = None
    llm_tools: bool = False  # LLM steps may call tools (settings.llm_tools); knowledge tools come over MCP
    supervisor: str = "auto"  # auto | llm | rules (auto: llm when the gateway calls a model, else rules)
    interactive: bool = False  # owner questions pause the run (needs a checkpointer to resume)
    max_steps: int = 6  # supervisor decisions per run
    budget_usd: float | None = None  # model spend per run (None: no limit)
    max_calls: int | None = None  # model calls per run (None: no limit)
    work: dict[str, Any] = field(default_factory=dict)  # per-run working objects, rebuilt when missing


def _ctx(state: PipelineState, runtime: Runtime[Deps]) -> AgentContext:
    d = runtime.context
    return AgentContext(kg=d.kg, artifacts=d.artifacts, run_id=state["run_id"], app_id=state["app_id"],
                        gateway=d.gateway)


def _stamp(agent: BaseAgent, metrics: dict[str, Any], t0: float) -> dict[str, Any]:
    """The metrics BaseAgent.__call__ adds, for an agent step the graph calls directly."""
    metrics.setdefault("agent", f"{agent.spec.name}@{agent.spec.version}")
    metrics["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return metrics


def _t(node: str, **kw: Any) -> list[dict[str, Any]]:
    return [{"node": node, "at": round(time.time(), 3), **kw}]


def _job(state: PipelineState) -> dict[str, Any]:
    return {"bi_ir_key": state.get("bi_ir_key"), "translation_key": state.get("translation_key"),
            "files": state.get("classified", []), "validation_key": state.get("validation_key"),
            "figures_key": state.get("figures_key"),
            "inputs": [{"name": f["name"], "sha256": f.get("sha256"), "class": f["class"]}
                       for f in state.get("classified", [])]}


def _need(state: PipelineState, key: str) -> Any:
    """A state key this node needs; an earlier node always sets it."""
    return state[key]  # type: ignore[literal-required]


def _tables(state: PipelineState) -> list[str]:
    return state.get("out_tables", [])


# ---------------------------------------------------------------- entry and the deterministic front
async def enter(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    return {"trace": _t("enter", start_at=state.get("start_at") or "intake")}


def _route_start(state: PipelineState) -> str:
    s = state.get("start_at") or "intake"
    return s if s in ENTRIES else "intake"


async def intake(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await IntakeRouter()({"app_id": state["app_id"], "app_name": state.get("app_name"),
                              "files": state["files"]}, _ctx(state, runtime))
    return {"classified": r.outputs["files"], "events": r.events, "metrics": {"intake": r.metrics},
            "trace": _t("intake", files={f["name"]: f["class"] for f in r.outputs["files"]})}


async def extract(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await BIExtractor()({"files": state.get("classified", [])}, _ctx(state, runtime))
    if "bi_ir_key" not in r.outputs:
        return {"status": "needs_inputs", "metrics": {"extract": r.metrics},
                "trace": _t("extract", stopped="no script files")}
    m = r.metrics
    return {"bi_ir_key": r.outputs["bi_ir_key"], "tables": r.outputs["tables"], "sources": r.outputs["sources"],
            "events": r.events, "metrics": {"extract": m},
            "trace": _t("extract", statements=m.get("statements"), coverage=m.get("coverage"),
                        tables=m.get("tables"), issues=m.get("issues"))}


def _after_extract(state: PipelineState) -> str:
    return END if state.get("status") == "needs_inputs" else "resolve"


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
    return {"pending": pending, "metrics": {"resolve": {"pending": len(pending)}},
            "trace": _t("resolve", pending=len(pending))}


def _after_resolve(state: PipelineState) -> str:
    return "synthesize" if state.get("reuse_translation") and state.get("translation_key") else "translate"


# ---------------------------------------------------------------- translate (subgraph, agent 28)
async def _work(state: PipelineState, runtime: Runtime[Deps]) -> TranslateWork:
    """Agent 28's working objects for this run; built again after a restart (the overrides come from the state)."""
    deps = runtime.context
    w = deps.work.get("translate")
    if w is None:
        w = await ScriptTranslator().prepare(_translate_job(state, runtime), _ctx(state, runtime))
        deps.work["translate"] = w
    return w


def _translate_job(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    return {"bi_ir_key": state.get("bi_ir_key"), "files": state.get("classified", []),
            "llm_tools": runtime.context.llm_tools}


def _overrides(tr: dict[str, Any]) -> dict[int, str]:
    return {int(k): v for k, v in tr["overrides"].items()}


async def t_prepare(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    runtime.context.work.pop("translate", None)
    runtime.context.work["translate_t0"] = time.perf_counter()
    w = await _work(state, runtime)
    overrides = dict(w.overrides)
    fixes = apply_rule_fixes(w.app, w.memory, overrides, w.validator, w.rejected)
    tr = {"overrides": {str(k): v for k, v in overrides.items()}, "tried": sorted(overrides), "queue": [],
          "round": 0, "outcomes": [asdict(o) for o in fixes], "current": None, "stop": False, "next": "pick"}
    return {"tr": tr, "trace": _t("translate.prepare", reviewer_sql=len(w.overrides),
                                  learned_corrections=[o.status for o in fixes], memory=len(w.memory.entries))}


async def t_pick(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tr = copy.deepcopy(_need(state, "tr"))
    w = await _work(state, runtime)
    if not tr["queue"]:
        if tr["stop"] or tr["round"] >= MAX_ROUNDS:
            tr["next"] = "finish"
            return {"tr": tr, "trace": _t("translate.pick", done="stopped" if tr["stop"] else "round limit")}
        roots = next_statements(w.app, _overrides(tr), set(tr["tried"]))
        if not roots:
            tr["next"] = "finish"
            return {"tr": tr, "trace": _t("translate.pick", done="nothing left to translate")}
        tr["queue"] = [asdict(u) for u in roots]  # the round's statements as composed now (the prompts use these)
        tr["round"] += 1
    u = tr["queue"].pop(0)
    tr["tried"] = sorted(set(tr["tried"]) | {u["statement"]})
    tr["current"] = {"u": u}
    tr["next"] = "memory"
    return {"tr": tr, "trace": _t("translate.pick", round=tr["round"], statement=u["statement"], table=u["table"],
                                  reason=u["reason"][:200])}


def _done(tr: dict[str, Any], o: Outcome, report: bool = True) -> None:
    if report:
        tr["outcomes"].append(asdict(o))
    if o.sql and o.status in ACCEPTED:
        tr["overrides"][str(o.statement)] = o.sql
    if o.status == "skipped":  # the provider is not answering: leave the rest for the next run
        tr["stop"], tr["queue"] = True, []
    tr["current"], tr["next"] = None, "pick"


async def t_memory(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tr = copy.deepcopy(_need(state, "tr"))
    w = await _work(state, runtime)
    u = Untranslated(**tr["current"]["u"])
    o, feedback, nxt = from_memory(w.app, u, _overrides(tr), w.memory, w.validator,
                                   has_model=runtime.context.gateway is not None, accepted_gaps=w.accepted_gaps,
                                   rejection=w.rejected.get(u.table))
    if nxt == "model":
        tr["current"] |= {"outcome": asdict(o), "feedback": feedback, "left": MAX_ATTEMPTS}
        tr["next"] = "propose"
    else:
        _done(tr, o, report=nxt == "done")
    return {"tr": tr, "trace": _t("translate.memory", statement=u.statement,
                                  result="ask the model" if nxt == "model" else o.status)}


async def t_propose(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tr = copy.deepcopy(_need(state, "tr"))
    w = await _work(state, runtime)
    cur = tr["current"]
    u, o, feedback = Untranslated(**cur["u"]), Outcome(**cur["outcome"]), cur["feedback"]
    overrides = _overrides(tr)
    inputs, kw = statement_request(w.app, u, w.memory, script_settings(w.app), feedback, overrides, w.validator,
                                   tools=w.use_tools, kg_tools=w.kg_tools)
    res, nxt = await call_model(runtime.context.gateway, "translate_statement", inputs, StatementTranslation, o,
                                task=f"{state['run_id']}:translate", kw=kw)
    verdict = nxt
    if res is not None:
        out = res.output
        assert isinstance(out, StatementTranslation)
        verdict = judge_statement(w.app, u, out, overrides, w.validator, w.memory, o, feedback)
    left = cur["left"] - 1
    last = o.attempts[-1] if o.attempts else {}
    if verdict in ("stop", "accepted", "declined") or left <= 0:
        _done(tr, o)
    else:
        cur |= {"outcome": asdict(o), "feedback": feedback, "left": left}
        tr["next"] = "propose"
    return {"tr": tr, "trace": _t("translate.propose", statement=u.statement, attempt=MAX_ATTEMPTS - left,
                                  verdict=verdict, status=o.status, level=last.get("level"),
                                  problems=(last.get("problems") or [])[:3], error=last.get("error"))}


async def t_finish(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tr = _need(state, "tr")
    w = await _work(state, runtime)
    agent = ScriptTranslator()
    r = await agent.finish(_translate_job(state, runtime), _ctx(state, runtime), w, _overrides(tr),
                           [Outcome(**d) for d in tr["outcomes"]])
    m = _stamp(agent, r.metrics, runtime.context.work.get("translate_t0", time.perf_counter()))
    return {"translation_key": r.outputs["translation_key"], "events": r.events, "metrics": {"translate": m},
            "trace": _t("translate.finish", untranslated=m.get("untranslated"), ai_overrides=m.get("ai_overrides"),
                        gaps=m.get("gap_analyses"), outcomes={d["statement"]: d["status"] for d in tr["outcomes"]})}


def _tr_next(state: PipelineState) -> str:
    return _need(state, "tr")["next"]


class TranslateIn(TypedDict, total=False):
    """What the translate subgraph receives. Not the trace, events or metrics: the subgraph returns only what it
    adds to them (the parent's reducers append it), so nothing is counted twice."""

    app_id: str
    run_id: str
    files: list[dict[str, Any]]
    classified: list[dict[str, Any]]
    bi_ir_key: str


def build_translate_graph() -> StateGraph[PipelineState, Deps, TranslateIn, PipelineState]:
    g = StateGraph(PipelineState, context_schema=Deps, input_schema=TranslateIn)
    g.add_node("prepare", t_prepare)
    g.add_node("pick", t_pick)
    g.add_node("memory", t_memory)
    g.add_node("propose", t_propose)
    g.add_node("finish", t_finish)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "pick")
    g.add_conditional_edges("pick", _tr_next, ["memory", "finish"])
    g.add_conditional_edges("memory", _tr_next, ["propose", "pick"])
    g.add_conditional_edges("propose", _tr_next, ["propose", "pick"])
    g.add_edge("finish", END)
    return g


# ---------------------------------------------------------------- synthesize, check (per table), repair
async def synthesize(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await Synthesizer()(_job(state), _ctx(state, runtime))
    loaded = load_translation(runtime.context.artifacts, _need(state, "bi_ir_key"),
                              state.get("translation_key"))
    m = {"tables": len(r.outputs["sql_keys"]), "sources_unmapped": r.metrics.get("sources_unmapped")}
    return {"events": r.events, "out_tables": output_tables(loaded), "metrics": {"synthesize": m},
            "trace": _t("synthesize", tables=sorted(r.outputs["sql_keys"]),
                        needs_dremio_dataset=len(r.outputs.get("needs_dremio_dataset", [])))}


def _fan_check(state: PipelineState) -> Any:
    tables = _tables(state)
    return [Send("check_one", {**state, "table": t}) for t in tables] if tables else "check_join"


async def check_one(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    t = _need(state, "table")
    loaded = load_translation(runtime.context.artifacts, _need(state, "bi_ir_key"), state.get("translation_key"))
    c = await QueryChecker().check_one(_ctx(state, runtime), AgentResult(), loaded, t)
    return {"checks": {t: c}, "trace": _t("check", table=t, ok=c["ok"], failing=c["failing"][:3])}


async def check_join(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tables = _tables(state)
    checks = state.get("checks", {})
    passed = sum(1 for t in tables if checks.get(t, {}).get("ok"))
    a = QueryChecker().spec
    return {"events": ["sql.checked"], "metrics": {"check": {"passed": passed, "agent": f"{a.name}@{a.version}"}},
            "trace": _t("check.join", passed=passed, tables=len(tables))}


def _failing(state: PipelineState) -> list[str]:
    checks = state.get("checks", {})
    return [t for t in _tables(state) if checks.get(t, {}).get("failing")]


def _fan_reconcile(state: PipelineState) -> Any:
    tables = _tables(state)
    return [Send("reconcile_one", {**state, "table": t}) for t in tables] if tables else "supervisor"


def _after_check(state: PipelineState) -> Any:
    loop = state.get("loop", {})
    if _failing(state) and state.get("has_model") and loop.get("repairs", 0) < REPAIR_ROUNDS:
        return "repair"
    return _fan_reconcile(state)


async def repair(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    tables = _failing(state)
    key, outcomes = await QueryChecker().repair({**_job(state)}, _ctx(state, runtime), tables)
    loop = dict(state.get("loop", {}))
    loop["repairs"] = loop.get("repairs", 0) + 1
    loop["repaired"] = bool(key)
    out: dict[str, Any] = {"loop": loop, "trace": _t("repair", tables=tables,
                                                     outcomes={o["table"]: o["status"] for o in outcomes})}
    if key:
        out["translation_key"] = key
    return out


def _after_repair(state: PipelineState) -> Any:
    return "synthesize" if state.get("loop", {}).get("repaired") else _fan_reconcile(state)


# ---------------------------------------------------------------- reconcile (per table), supervisor, diagnose, ask
async def reconcile_one(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    t = _need(state, "table")
    e = ReconcilerAgent().validate(_job(state), _ctx(state, runtime), t)
    return {"validations": {t: e}, "trace": _t("reconcile", table=t, level=e["level"],
                                               mismatch=e["mismatch"]["category"],
                                               detail=e["mismatch"]["detail"][:200])}


async def _owner_questions(kg: Any, app_id: str) -> list[str]:
    return [f.value.get("message", "") for f in await kg.facts(kind="bi_issue", subject=f"app:{app_id}")
            if f.value.get("code") == "unresolved_variable"]


async def supervisor(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    deps = runtime.context
    loop = dict(state.get("loop", {}))
    steps = loop.get("steps", 0)
    all_v = state.get("validations", {})
    validations = {t: all_v[t] for t in _tables(state) if t in all_v}
    diagnosed = loop.get("diagnosed", {})
    questions = await _owner_questions(deps.kg, state["app_id"]) if deps.interactive and not loop.get("asked") \
        else []
    opts = options(validations, diagnosed, has_model=deps.gateway is not None, can_ask=bool(questions))
    mode = deps.supervisor
    if mode == "auto":
        mode = "llm" if deps.gateway is not None and getattr(deps.gateway, "mode", "") in ("live", "record") \
            else "rules"
    if steps >= deps.max_steps:
        choice = {"action": "publish", "table": None, "reason": f"step limit ({deps.max_steps}) reached",
                  "by": "rules"}
    elif len(opts) == 1:
        choice = {**opts[0], "reason": "the only option", "by": "rules"}
    elif mode == "llm" and deps.gateway is not None:
        left = deps.gateway.left() if isinstance(deps.gateway, RunGateway) else {}
        choice = await by_llm(deps.gateway, plan_inputs(validations, diagnosed, opts, deps.max_steps - steps, left,
                                                        questions), opts, task=f"{state['run_id']}:plan")
    else:
        choice = by_rules(opts)
    loop["steps"] = steps + 1
    decision = {"step": steps + 1, **choice, "options": [f"{o['action']}:{o['table']}" if o["table"] else o["action"]
                                                         for o in opts]}
    return {"loop": loop, "next": choice, "decisions": [decision], "questions": questions,
            "trace": _t("supervisor", **decision)}


def _after_supervisor(state: PipelineState) -> str:
    return {"diagnose": "diagnose", "ask_owner": "ask_owner"}.get(_need(state, "next")["action"], "record")


async def diagnose(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    t = _need(state, "next")["table"]
    job = {**_job(state), "llm_tools": runtime.context.llm_tools}
    key, outcomes = await ReconcilerAgent().diagnose(job, _ctx(state, runtime), AgentResult(), [t])
    loop = copy.deepcopy(state.get("loop", {}))
    loop.setdefault("diagnosed", {})[t] = loop.get("diagnosed", {}).get(t, 0) + 1
    loop["fixed"] = bool(key)
    o = outcomes[0] if outcomes else {}
    out: dict[str, Any] = {"loop": loop, "trace": _t("diagnose", table=t, status=o.get("status", "nothing to fix"),
                                                     model_calls=o.get("model_calls"), notes=o.get("notes", "")[:300])}
    if key:
        out["translation_key"] = key
    return out


def _after_diagnose(state: PipelineState) -> str:
    return "synthesize" if state.get("loop", {}).get("fixed") else "supervisor"


async def ask_owner(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Pause the run until the owner answers (the API records the answers as review decisions, then resumes the
    run with {"answered": [...]}); with answers the run extracts again, so the script is read with them."""
    answer = interrupt({"app_id": state["app_id"], "run_id": state["run_id"], "questions": state.get("questions", [])})
    loop = dict(state.get("loop", {}))
    loop["asked"] = True
    loop["answered"] = bool(isinstance(answer, dict) and answer.get("answered"))
    return {"loop": loop, "trace": _t("ask_owner", questions=len(state.get("questions", [])),
                                      answered=loop["answered"])}


def _after_ask(state: PipelineState) -> str:
    return "extract" if state.get("loop", {}).get("answered") else "supervisor"


# ---------------------------------------------------------------- record, publish, summarize
async def record(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    t0 = time.perf_counter()
    agent = ReconcilerAgent()
    r = AgentResult()
    v = state.get("validations", {})
    await agent.record(_job(state), _ctx(state, runtime), r, [v[t] for t in _tables(state) if t in v])
    return {"validation_key": r.outputs["validation_key"], "figures_key": r.outputs["figures_key"],
            "levels": r.outputs["levels"], "events": r.events, "metrics": {"reconcile": _stamp(agent, r.metrics, t0)},
            "trace": _t("record", levels=r.outputs["levels"])}


async def publish(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    r = await Publisher()(_job(state), _ctx(state, runtime))
    return {"pack_prefix": r.outputs["pack_prefix"], "events": r.events, "metrics": {"publish": r.metrics},
            "trace": _t("publish", files=len(r.outputs.get("files", {})))}


SUMMARY_KEYS = ("app_id", "run_id", "classified", "bi_ir_key", "translation_key", "tables", "sources", "pending",
                "levels", "pack_prefix", "events", "metrics", "decisions", "trace")


async def summarize(state: PipelineState, runtime: Runtime[Deps]) -> dict[str, Any]:
    status = "waiting_for_dremio" if state.get("pending") else "complete"
    summary = {k: state.get(k) for k in SUMMARY_KEYS} | {"status": status}
    store = runtime.context.artifacts
    key = put_json(store, f"apps/{state['app_id']}/runs/{state['run_id']}/summary.json", summary)
    put_json(store, f"apps/{state['app_id']}/latest.json", {"run_id": state["run_id"], "summary_key": key})
    return {"status": status, "metrics": {"summary_key": key}, "trace": _t("summarize", status=status)}


# ---------------------------------------------------------------- graph
def build_graph() -> StateGraph[PipelineState, Deps, PipelineState, PipelineState]:
    g = StateGraph(PipelineState, context_schema=Deps)
    for name, fn in (("enter", enter), ("intake", intake), ("extract", extract), ("resolve", resolve),
                     ("synthesize", synthesize), ("check_one", check_one), ("check_join", check_join),
                     ("repair", repair), ("reconcile_one", reconcile_one), ("supervisor", supervisor),
                     ("diagnose", diagnose), ("ask_owner", ask_owner), ("record", record), ("publish", publish),
                     ("summarize", summarize)):
        g.add_node(name, fn)  # type: ignore[call-overload]
    g.add_node("translate", build_translate_graph().compile())
    g.add_edge(START, "enter")
    g.add_conditional_edges("enter", _route_start, list(ENTRIES))
    g.add_edge("intake", "extract")
    g.add_conditional_edges("extract", _after_extract, ["resolve", END])
    g.add_conditional_edges("resolve", _after_resolve, ["translate", "synthesize"])
    g.add_edge("translate", "synthesize")
    g.add_conditional_edges("synthesize", _fan_check, ["check_one", "check_join"])
    g.add_edge("check_one", "check_join")
    g.add_conditional_edges("check_join", _after_check, ["repair", "reconcile_one", "supervisor"])
    g.add_conditional_edges("repair", _after_repair, ["synthesize", "reconcile_one", "supervisor"])
    g.add_edge("reconcile_one", "supervisor")
    g.add_conditional_edges("supervisor", _after_supervisor, ["diagnose", "ask_owner", "record"])
    g.add_conditional_edges("diagnose", _after_diagnose, ["synthesize", "supervisor"])
    g.add_conditional_edges("ask_owner", _after_ask, ["extract", "supervisor"])
    g.add_edge("record", "publish")
    g.add_edge("publish", "summarize")
    g.add_edge("summarize", END)
    return g


def _deps_for_run(deps: Deps) -> Deps:
    gw = deps.gateway
    if gw is not None and not isinstance(gw, RunGateway):
        gw = RunGateway(gw, max_usd=deps.budget_usd, max_calls=deps.max_calls)
    return replace(deps, gateway=gw, work={})


def _config(app_id: str, run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": f"{app_id}:{run_id}"}, "recursion_limit": 1000}


async def run_pipeline(*, app_id: str, run_id: str, files: list[dict[str, Any]], deps: Deps,
                       checkpointer: Any | None = None, app_name: str | None = None, start_at: str | None = None,
                       previous: dict[str, Any] | None = None, reuse_translation: bool = False) -> PipelineState:
    """One run. start_at/previous: a re-run entering at a later node with the previous run's summary (its
    classified files, BI IR, translation, tables and sources). A run paused for owner questions returns its state
    with "__interrupt__"; resume_pipeline continues it."""
    graph = build_graph().compile(checkpointer=checkpointer)
    run_deps = _deps_for_run(deps)
    state: PipelineState = {"app_id": app_id, "run_id": run_id, "files": files, "events": [],
                            "metrics": {}, "trace": [], "decisions": [], "has_model": run_deps.gateway is not None}
    if app_name:
        state["app_name"] = app_name
    if start_at and start_at != "intake" and previous:
        state["start_at"] = start_at
        state["reuse_translation"] = reuse_translation
        for k in ("classified", "bi_ir_key", "translation_key", "tables", "sources"):
            if previous.get(k) is not None:
                state[k] = previous[k]  # type: ignore[literal-required]
    return await graph.ainvoke(state, config=_config(app_id, run_id), context=run_deps)  # type: ignore[return-value]


async def resume_pipeline(*, app_id: str, run_id: str, deps: Deps, checkpointer: Any,
                          answer: dict[str, Any]) -> PipelineState:
    """Continue a run paused in ask_owner, e.g. with {"answered": ["<review item id>", ...]}."""
    graph = build_graph().compile(checkpointer=checkpointer)
    return await graph.ainvoke(Command(resume=answer), config=_config(app_id, run_id),  # type: ignore[return-value]
                               context=_deps_for_run(deps))
