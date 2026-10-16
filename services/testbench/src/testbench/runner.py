"""Run any agent, or any combination of agents, on a case, and check the results against the case's expectations.

Agents run in pipeline order on one in-memory knowledge port and artifact store, exactly as the orchestrator calls
them. Selecting an agent adds the agents it needs (marked `auto`), so "26 alone" means 1 -> 27 -> 28 -> 26 with only
26 under test. Chain 0 runs the real LangGraph pipeline (orchestrator) instead, to test the wiring itself.

Each agent's step records: its events, metrics, outputs, the facts it proposed and the artifacts it wrote (text
shown), or the exception it raised. Checks then compare those with the case: classes, tables, fields, issues, SQL
fragments, rule checks, validation levels (capped by the data variant), and the rows of each table against the
hand-computed expected rows.
"""
from __future__ import annotations

import json
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adce_core.agent import AgentContext, BaseAgent
from adce_core.artifacts import LocalArtifactStore
from adce_core.ports import MemoryKG

from .cases import LEVELS, ROOT, Case

# number -> (step name, class path, prerequisites)
AGENTS: dict[int, tuple[str, str, list[int]]] = {
    1: ("intake", "agents_ingestion.intake_router:IntakeRouter", []),
    27: ("extract", "agents_bi_adapters.bi_extractor:BIExtractor", [1]),
    28: ("translate", "agents_bi_adapters.script_translator:ScriptTranslator", [27]),
    24: ("synthesize", "agents_reversesql.agents:Synthesizer", [28]),
    25: ("check", "agents_reversesql.agents:QueryChecker", [28]),
    26: ("reconcile", "agents_reversesql.agents:ReconcilerAgent", [28]),
    20: ("publish", "agents_reversesql.agents:Publisher", [26]),
    23: ("joins", "agents_reversesql.frontend_agents:JoinPlanner", [28]),
    29: ("measures", "agents_bi_adapters.expression_translator:ExpressionTranslator", [28]),
    21: ("reports", "agents_reversesql.frontend_agents:ReportParser", [1]),
    22: ("metrics", "agents_reversesql.frontend_agents:MetricMapper", [21, 28]),
}
ORDER = [1, 27, 28, 24, 25, 26, 20, 23, 29, 21, 22]
PIPELINE = 0  # the LangGraph orchestrator as one step


def _cls(path: str) -> type[BaseAgent]:
    import importlib

    mod, name = path.split(":")
    return getattr(importlib.import_module(mod), name)


def agent_catalog() -> list[dict[str, Any]]:
    out = []
    for n in ORDER:
        step, path, needs = AGENTS[n]
        spec = _cls(path).spec
        out.append({"number": n, "step": step, "name": spec.name, "kind": spec.kind.value, "needs": needs,
                    "routes": spec.routes, "writes": spec.writes})
    out.append({"number": PIPELINE, "step": "pipeline", "name": "orchestrator (LangGraph qlik_pipeline)",
                "kind": "D", "needs": [], "routes": [], "writes": []})
    return out


def resolve_chain(selected: list[int]) -> list[tuple[int, bool]]:
    """(agent, auto) in pipeline order: the selected agents plus the ones they need."""
    if PIPELINE in selected:
        return [(PIPELINE, False)]
    need: set[int] = set()

    def add(n: int) -> None:
        for p in AGENTS[n][2]:
            if p not in need:
                need.add(p)
                add(p)

    for n in selected:
        add(n)
    return [(n, n not in selected) for n in ORDER if n in selected or n in need]


@dataclass
class Variant:
    ai: str = "off"  # off | replay | live
    data: str = "full"
    tools: bool = False  # LLM steps may call tools (check_sql, get_statements, kg-mcp lookups)
    model: str = ""  # live: a gateway config in deploy/gateway (e.g. claude-code); "" = ADCE_GATEWAY_CONFIG

    def label(self) -> str:
        model = f" model={self.model}" if self.model and self.ai == "live" else ""
        return f"ai={self.ai}{'+tools' if self.tools else ''}{model} data={self.data}"


@dataclass
class Check:
    section: str
    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    note: str = ""


@dataclass
class RunResult:
    case: str
    variant: Variant
    chain: list[int]
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "pass"  # pass | fail | error | xfail | xpass
    seconds: float = 0.0
    gateway: dict[str, Any] = field(default_factory=dict)
    kg_calls: int = 0  # knowledge reads and writes made over MCP

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "variant": self.variant.__dict__, "chain": self.chain, "steps": self.steps,
                "checks": [c.__dict__ for c in self.checks], "rows": self.rows, "status": self.status,
                "seconds": round(self.seconds, 2), "gateway": self.gateway, "kg_calls": self.kg_calls,
                "passed": sum(c.ok for c in self.checks), "failed": sum(not c.ok for c in self.checks)}


GATEWAYS = ROOT / "deploy" / "gateway"


def gateway_choices() -> list[dict[str, Any]]:
    """The gateway configs a live run can use (deploy/gateway/*.toml): the model of each tier, and for configs that
    call a local endpoint (the Claude Code bridge) whether it answers."""
    import os
    import tomllib
    import urllib.request

    from adce_reversesql.cli import _env_file

    _env_file(str(ROOT / "deploy" / "compose" / ".env"))  # keys the configs name (never returned)
    out = []
    for p in sorted(GATEWAYS.glob("*.toml")):
        try:
            cfg = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        local = [v["base_url"] for v in cfg.get("providers", {}).values()
                 if str(v.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost"))]
        row: dict[str, Any] = {"name": p.stem, "model": cfg.get("models", {}).get("standard", ""),
                               "local_url": local[0] if local else None, "running": None,
                               "bridge": bool(local) and ":8790" in local[0],  # the Claude Code bridge's port
                               # environment variables the config needs that are not set (keys, project ids)
                               "missing": sorted({v[k] for v in cfg.get("providers", {}).values()
                                                  for k in ("api_key_env", "project_env", "base_url_env")
                                                  if v.get(k) and not os.environ.get(v[k])})}
        if local:
            try:
                with urllib.request.urlopen(local[0].removesuffix("/v1") + "/health", timeout=1) as r:
                    row["running"] = r.status == 200
            except OSError:
                row["running"] = False
        out.append(row)
    return out


def make_gateway(ai: str, recordings: Path | None = None, model: str = "") -> Any | None:
    """off: no model (rules only). replay: recorded answers in eval/recordings, no key, no network.
    live: the configured provider (deploy/compose/.env, or `model`: a config in deploy/gateway), answers recorded
    in .adce/recordings and reused."""
    if ai == "off":
        return None
    from adce_gateway.config import default_config, load_config
    from adce_gateway.gateway import ModelGateway, Recordings

    if ai == "replay":
        return ModelGateway("replay", Recordings(ROOT / "eval" / "recordings"),
                            config=load_config(ROOT / "deploy" / "gateway" / "groq-free.toml"))
    from adce_core.settings import Settings
    from adce_reversesql.cli import _env_file

    _env_file(str(ROOT / "deploy" / "compose" / ".env"))
    s = Settings()
    if model:
        path = GATEWAYS / f"{model}.toml"
        if not path.is_file():
            raise ValueError(f"no gateway config {model} in {GATEWAYS}")
        config = load_config(path)
    else:
        config = load_config(s.gateway_config) if s.gateway_config else default_config(s.vertex_region)
    return ModelGateway("record", Recordings(recordings or ROOT / ".adce" / "recordings"), config=config)


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


def _artifact_view(store: LocalArtifactStore, key: str) -> dict[str, Any]:
    try:
        data = store.get(key)
    except Exception:  # noqa: BLE001
        return {"key": key, "text": None}
    text = None
    if key.endswith((".sql", ".md", ".json", ".txt")) and len(data) < 300_000:
        text = data.decode("utf-8", errors="replace")
    return {"key": key, "bytes": len(data), "text": text}


class Runner:
    def __init__(self, case: Case, variant: Variant, selected: list[int] | None = None,
                 kg: MemoryKG | None = None) -> None:
        self.case = case
        self.variant = variant
        self.selected = selected or case.chain
        self.kg = kg or MemoryKG()  # pass a shared one to test memory across runs
        self.tmp = Path(tempfile.mkdtemp(prefix="adce-bench-"))
        self.store = LocalArtifactStore(self.tmp / "artifacts")
        self.state: dict[str, Any] = {}
        self.recordings: Path | None = None  # live runs: where answers are recorded (evaluation: a fresh folder)

    def upload(self) -> list[dict[str, Any]]:
        files = []
        for name, p in self.case.files(self.variant.data):
            files.append({"name": name, "key": self.store.put(f"uploads/{self.case.id}/{name}", p.read_bytes())})
        return files

    async def run(self) -> RunResult:
        t0 = time.perf_counter()
        chain = resolve_chain(self.selected)
        res = RunResult(case=self.case.id, variant=self.variant, chain=[n for n, _ in chain])
        app_id = self.case.id.replace("/", "_")
        gateway = make_gateway(self.variant.ai, self.recordings, self.variant.model)
        calls: list[dict[str, Any]] = []
        if gateway is not None:
            _log_calls(gateway, calls)
        self.state = {"files": self.upload(), "app_id": app_id}
        from kg_mcp.client import McpKG

        # agents reach knowledge through kg-mcp (ADR-2), here an in-process server over the run's store
        async with McpKG.serving(self.kg) as kg:
            ctx = AgentContext(kg=kg, artifacts=self.store, run_id="bench", app_id=app_id, gateway=gateway)
            for n, auto in chain:
                step = await (self._pipeline(ctx) if n == PIPELINE else self._step(n, auto, ctx))
                res.steps.append(step)
                if step["error"]:
                    break
            res.kg_calls = kg.calls
        if gateway is not None:
            res.gateway = {"mode": gateway.mode, "calls": calls}
        self._check(res)
        res.seconds = time.perf_counter() - t0
        failed = any(not c.ok for c in res.checks) or any(s["error"] for s in res.steps)
        errored = any(s["error"] for s in res.steps)
        if self.case.status == "known_gap":
            res.status = "xfail" if failed else "xpass"
        else:
            res.status = "error" if errored else "fail" if failed else "pass"
        return res

    def _job(self, n: int) -> dict[str, Any]:
        s = self.state
        files = s.get("classified", [])
        if n == 1:
            return {"app_id": s["app_id"], "files": s["files"]}
        if n == 27:
            return {"files": files}
        if n == 28:
            return {"bi_ir_key": s.get("bi_ir_key"), "files": files, "llm_tools": self.variant.tools}
        if n == 21:
            return {"files": files}
        job = {"bi_ir_key": s.get("bi_ir_key"), "translation_key": s.get("translation_key"), "files": files,
               "validation_key": s.get("validation_key"), "figures_key": s.get("figures_key"),
               "inputs": [{"name": f["name"], "sha256": f.get("sha256"), "class": f["class"]} for f in files]}
        if n == 22:
            job["exports"] = s.get("exports", [])
        if self.case.dremio_sources:
            job["source_map"] = self.case.dremio_sources
        return job

    async def _step(self, n: int, auto: bool, ctx: AgentContext) -> dict[str, Any]:
        step_name, path, _ = AGENTS[n]
        agent = _cls(path)()
        job = self._job(n)
        out: dict[str, Any] = {"number": n, "step": step_name, "name": agent.spec.name, "kind": agent.spec.kind.value,
                               "auto": auto, "error": None, "job": _jsonable({k: v for k, v in job.items()
                                                                              if k not in ("files", "inputs")})}
        before = len(await self.kg.facts())
        try:
            r = await agent(job, ctx)
        except Exception as e:  # noqa: BLE001 - shown on the bench
            out["error"] = f"{type(e).__name__}: {e}"
            out["traceback"] = traceback.format_exc()
            return out
        self._absorb(n, r.outputs)
        flush = getattr(ctx.kg, "flush", None)
        if flush is not None:
            await flush()  # McpKG buffers writes
        facts = (await self.kg.facts())[before:]
        keys = list(r.artifacts) + [v for k in ("sql_keys", "chart_keys", "files")
                                    if isinstance(r.outputs.get(k), dict) for v in r.outputs[k].values()]
        out.update({"events": r.events, "metrics": _jsonable(r.metrics), "outputs": _jsonable(r.outputs),
                    "facts": [{"kind": f.kind, "subject": f.subject, "object": f.object, "trust": f.trust.value,
                               "value": _jsonable(f.value)} for f in facts],
                    "artifacts": [_artifact_view(self.store, k) for k in dict.fromkeys(keys)]})
        return out

    def _absorb(self, n: int, o: dict[str, Any]) -> None:
        s = self.state
        if n == 1:
            s["classified"] = o["files"]
        for k in ("bi_ir_key", "translation_key", "validation_key", "figures_key", "levels", "tables", "sources",
                  "exports", "sql_keys", "pack_prefix", "files"):
            if k in o and not (n == 1 and k == "files"):
                s["pack_files" if k == "files" else k] = o[k]

    async def _pipeline(self, ctx: AgentContext) -> dict[str, Any]:
        from orchestrator.pipeline import Deps, run_pipeline

        out: dict[str, Any] = {"number": PIPELINE, "step": "pipeline", "name": "orchestrator", "kind": "D",
                               "auto": False, "error": None}
        try:
            st = dict(await run_pipeline(app_id=self.state["app_id"], run_id="bench", files=self.state["files"],
                                         deps=Deps(kg=ctx.kg, artifacts=self.store, gateway=ctx.gateway,
                                                   llm_tools=self.variant.tools)))
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
            out["traceback"] = traceback.format_exc()
            return out
        self.state.update({k: st[k] for k in ("classified", "bi_ir_key", "translation_key", "validation_key",
                                               "levels", "tables", "sources", "pack_prefix") if k in st})
        out.update({"events": st.get("events", []), "metrics": _jsonable(st.get("metrics", {})),
                    "outputs": _jsonable({k: v for k, v in st.items() if k not in ("files", "classified", "events",
                                                                                    "metrics")}),
                    "facts": [], "artifacts": []})
        return out

    # ------------------------------------------------------------------ checks
    def _check(self, res: RunResult) -> None:
        expect, levels = self.case.expectations(self.variant.ai, self.variant.data)
        ran = {s["step"]: s for s in res.steps if not s["error"]}
        if "pipeline" in ran:
            for k in ("intake", "extract", "translate", "synthesize", "check", "reconcile", "publish"):
                ran.setdefault(k, ran["pipeline"])
        add = res.checks.append

        def facts(step: str, kind: str) -> list[dict[str, Any]]:
            return [f for f in ran[step].get("facts", []) if f["kind"] == kind]

        if "intake" in ran and "intake" in expect and "pipeline" not in ran:
            got = {f["name"]: f["class"] for f in self.state.get("classified", [])}
            for name, cls in expect["intake"].get("classes", {}).items():
                add(Check("intake", f"class of {name}", got.get(name) == cls, cls, got.get(name)))
        if "extract" in ran and "extract" in expect:
            e = expect["extract"]
            tables = self.state.get("tables", [])
            if "tables" in e:
                add(Check("extract", "tables", sorted(tables) == sorted(e["tables"]), sorted(e["tables"]),
                          sorted(tables)))
            if "pipeline" not in ran:
                bt = {f["subject"].rsplit("table:", 1)[-1]: f["value"] for f in facts("extract", "bi_table")}
                for t, flds in e.get("fields", {}).items():
                    got = bt.get(t, {}).get("fields")
                    add(Check("extract", f"fields of {t}", got == flds, flds, got))
                if "issues" in e:
                    codes = sorted({f["value"]["code"] for f in facts("extract", "bi_issue")})
                    add(Check("extract", "issue codes", codes == sorted(e["issues"]), sorted(e["issues"]), codes))
                if "coverage" in e:
                    cov = ran["extract"]["metrics"].get("coverage")
                    add(Check("extract", "coverage", cov is not None and cov >= e["coverage"], e["coverage"], cov))
            if "sources" in e:
                got = sorted(self.state.get("sources", []))
                add(Check("extract", "sources", got == sorted(e["sources"]), sorted(e["sources"]), got))
        if "translate" in ran:
            e = expect.get("translate", {})
            m = ran["translate"].get("metrics", {})
            if "untranslated" in e and "pipeline" not in ran:
                add(Check("translate", "untranslated statements", m.get("untranslated") == e["untranslated"],
                          e["untranslated"], m.get("untranslated")))
            if "ai_status" in e:
                tr = self._translation_doc()
                got = {o["table"]: o["status"] for o in tr.get("ai_outcomes", [])}
                add(Check("translate", "AI outcomes", got == e["ai_status"], e["ai_status"], got))
        if "synthesize" in ran and "pipeline" not in ran:
            for t, parts in expect.get("synthesize", {}).get("sql_contains", {}).items():
                sql = self._sql(t)
                for p in parts:
                    add(Check("synthesize", f"{t} SQL contains {p}", p in (sql or ""), p, None if sql else "no SQL"))
        if "check" in ran and "pipeline" not in ran:
            for f in facts("check", "sql_checked"):
                t = f["subject"].rsplit("table:", 1)[-1]
                bad = [r["name"] for r in f["value"]["rules"] if not r["ok"] and r["blocking"]]
                want = expect.get("check", {}).get("failing", {}).get(t, [])
                add(Check("check", f"{t} blocking rule failures", sorted(bad) == sorted(want), want, bad))
        if "reconcile" in ran and levels:
            got = self.state.get("levels", {})
            for t, lv in levels.items():
                add(Check("reconcile", f"{t} level", got.get(t) == lv, lv, got.get(t),
                          "" if self.variant.data == "full" else f"capped for data={self.variant.data}"))
        for step, e in expect.items():
            if isinstance(e, dict) and "metrics" in e and step in ran:
                m = ran[step].get("metrics", {})
                if ran[step]["step"] == "pipeline":  # the orchestrator keeps each node's metrics under its name
                    m = m.get(step, {})
                for k, v in e["metrics"].items():
                    add(Check(step, f"metric {k}", m.get(k) == v, v, m.get(k)))
        if "publish" in ran and "files" in expect.get("publish", {}):
            got = sorted(self.state.get("pack_files", {}))
            add(Check("publish", "pack files", got == sorted(expect["publish"]["files"]),
                      sorted(expect["publish"]["files"]), got))
        if "publish" in ran and self.state.get("pack_prefix"):
            for name, parts in expect.get("publish", {}).get("pack_contains", {}).items():
                try:
                    text = self.store.get(f"{self.state['pack_prefix']}/{name}").decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    text = None
                for p in parts:
                    add(Check("publish", f"{name} contains {p!r}", text is not None and p in text, p,
                              "file missing" if text is None else None))
        rows_wanted = expect.get("rows", True) is not False
        if "translate" in ran and self.variant.data in ("full", "no_qvd", "no_log", "script_only") and rows_wanted:
            self._rows(res)

    def _translation_doc(self) -> dict[str, Any]:
        key = self.state.get("translation_key")
        return json.loads(self.store.get(key)) if key else {}

    def _loaded(self) -> Any:
        from adce_reversesql.runtime import load_translation

        return load_translation(self.store, self.state["bi_ir_key"], self.state.get("translation_key"))

    def _sql(self, table: str) -> str | None:
        try:
            return self._loaded().translation.table_sql(table)
        except Exception:  # noqa: BLE001
            return None

    def _rows(self, res: RunResult) -> None:
        """Run each expected table's SQL in the local venue and compare its rows with the hand-computed ones."""
        if "bi_ir_key" not in self.state:
            return
        from adce_reversesql.runtime import local_evidence
        from agents_reversesql.agents import output_tables

        expected = self.case.expected_rows()
        loaded = self._loaded()
        venue = local_evidence(self.store, self.state.get("classified", []), loaded.app).venue
        shown: dict[str, tuple[list[str], list[tuple[Any, ...]]] | None] = dict(expected)
        for t in output_tables(loaded):
            shown.setdefault(t, None)  # no expected rows: show the output, no check
        for table, exp_rows in shown.items():
            cols, rows = exp_rows if exp_rows is not None else ([], [])
            view: dict[str, Any] = {"columns": cols, "expected": [list(r) for r in rows] if exp_rows else None}
            sql = loaded.translation.table_sql(table) if table in loaded.translation.tables else None
            if exp_rows is None and (sql is None or self.variant.data == "script_bare"):
                continue
            if sql is None:
                view.update({"ok": False, "error": "no SQL for this table"})
            else:
                try:
                    r = venue.run(loaded.translation.render(loaded.translation.tables[table].final_cte or "",
                                                            comments=False), f"bench:{table}")
                    got_cols = list(r.columns)
                    view["actual_columns"] = got_cols
                    view["actual"] = [[_cell(v) for v in row] for row in r.rows]
                    if exp_rows is None:
                        view.update({"ok": None, "columns": got_cols})
                    elif set(got_cols) != set(cols):
                        view.update({"ok": False, "error": f"columns differ: expected {sorted(cols)}, "
                                                           f"got {sorted(got_cols)}"})
                    else:
                        idx = [got_cols.index(c) for c in cols]
                        mine = sorted((_key(tuple(row[i] for i in idx)) for row in r.rows), key=repr)
                        theirs = sorted((_key(row) for row in rows), key=repr)
                        missing, extra = _diff(theirs, mine)
                        view.update({"ok": not missing and not extra, "missing": missing[:50], "extra": extra[:50]})
                except Exception as e:  # noqa: BLE001
                    view.update({"ok": False, "error": f"execution: {str(e).splitlines()[0]}"})
            res.rows[table] = view
            if exp_rows is None:
                continue
            res.checks.append(Check("rows", f"{table} rows", view["ok"], f"{len(rows)} rows",
                                    view.get("error") or f"{len(view.get('actual', []))} rows, "
                                    f"{len(view.get('missing', []))} missing, {len(view.get('extra', []))} extra"))


def _log_calls(gateway: Any, calls: list[dict[str, Any]]) -> None:
    """Record every model-gateway call of the run (route, model, replayed or live, tokens, latency, error)."""
    inner = gateway.call

    async def call(route: str, inputs: dict[str, Any], output_model: Any, **kw: Any) -> Any:
        entry: dict[str, Any] = {"route": route, "task": kw.get("task")}
        try:
            r = await inner(route, inputs, output_model, **kw)
        except Exception as e:  # noqa: BLE001 - recorded, then re-raised for the agent to handle
            entry["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            u = getattr(e, "usage", None)  # a tool conversation that ended without an answer still used tokens
            entry.update({"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens",
                          None), "tools": getattr(e, "tool_calls", None)})
            calls.append(entry)
            raise
        u = r.usage
        entry.update({"model": r.model, "provider": r.provider, "source": r.source, "latency_ms": r.latency_ms,
                      "turns": r.turns, "tools": r.tool_calls,
                      "input_tokens": getattr(u, "input_tokens", None),
                      "output_tokens": getattr(u, "output_tokens", None), "cost_usd": r.cost_usd,
                      "output": _jsonable(r.output.model_dump())})
        calls.append(entry)
        return r

    gateway.call = call


def _cell(v: Any) -> Any:
    from decimal import Decimal

    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def _key(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """A row as Qlik would show it: numbers in their shortest form (20, not 20.0), text as is, empty = NULL. A
    number and the same digits as text compare equal (Qlik values are duals); '20.0' and 20 do not."""
    out: list[Any] = []
    for v in row:
        v = _cell(v)
        if v == "" or v is None:
            out.append(None)
        elif isinstance(v, bool):
            out.append(str(-1 if v else 0))
        elif isinstance(v, (int, float)):
            out.append(repr(round(float(v), 9)).removesuffix(".0") if not float(v).is_integer() else str(int(v)))
        else:
            out.append(str(v))
    return tuple(out)


def _diff(expected: list[tuple[Any, ...]], actual: list[tuple[Any, ...]]) -> tuple[list[Any], list[Any]]:
    from collections import Counter

    e, a = Counter(expected), Counter(actual)
    return [list(k) for k in (e - a).elements()], [list(k) for k in (a - e).elements()]


async def run_case(case: Case, variant: Variant, selected: list[int] | None = None,
                   kg: MemoryKG | None = None) -> RunResult:
    return await Runner(case, variant, selected, kg).run()


def default_variants(case: Case) -> list[Variant]:
    return [Variant(ai, data) for ai in case.variants.get("ai", ["off"])
            for data in case.variants.get("data", ["full"])]


def level_ok(level: str | None) -> bool:
    return level in LEVELS
