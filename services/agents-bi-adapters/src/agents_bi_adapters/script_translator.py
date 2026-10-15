"""Agent 28: BI script translator. Load-script statements -> Dremio SQL (CTE chains).

Rules first (adce_reversesql.compose); reviewer SQL next; then the LLM pathway (adce_reversesql.ai_loop): translation
memory (learned and reviewer answers, also used with AI off), then the model gateway route translate_statement with
the statement text and its input columns only (never rows). Every proposal is checked by code and, when the
uploads hold data (reload log, QVDs, source extracts), validated locally; failures go back to the model with the
problems found. Accepted translations are stored as translation_memory facts and reused by later runs of any app.
AI-assisted SQL stays marked for review. Reviewer decisions steer it too (adce_reversesql.ai_loop, ADR-5).
"""
from __future__ import annotations

from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import content_key, put_json
from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_reversesql.ai_loop import (
    MemoryEntry,
    TranslationMemory,
    memory_key,
    memory_stats,
    repair_with_ai,
    translate_with_ai,
)
from adce_reversesql.compose import compose
from adce_reversesql.runtime import load_translation, validator_from_files

from .bi_extractor import table_key


class ScriptTranslator(BaseAgent):
    spec = AgentSpec(number=28, name="bi_script_translator", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_ir.ready"], emits=["bi_transform.ready"],
                     writes=["bi_transform", "translation_memory", "memory_check"],
                     routes=["translate_statement", "diagnose_mismatch"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"])
        t = loaded.translation
        overrides: dict[int, str] = {}
        ai_notes: dict[int, str] = {}
        reviewed = {f.value.get("span"): f.value["sql"] for f in await ctx.kg.facts(kind="translation_override")
                    if f.trust == Trust.CONFIRMED and f.subject.startswith(f"app:{ctx.app_id}/")}
        for r in t.tables.values():
            for u in r.untranslated:
                if u.span in reviewed:
                    overrides[u.statement] = reviewed[u.span]
                    ai_notes[u.statement] = "reviewer-provided SQL"
        if overrides:
            t = compose(loaded.app, overrides)
        ai_outcomes: list[dict[str, Any]] = []
        # learned and reviewer answers apply without a model too (AI off: gateway None); the model is asked only
        # for what the memory does not hold
        memory = await self._memory(ctx)
        known = {id(e) for e in memory.entries}
        validator = validator_from_files(ctx.artifacts, job["files"], loaded.app) if job.get("files") else None
        use_tools = bool(job.get("llm_tools")) and ctx.gateway is not None
        # read-only knowledge tools come from kg-mcp when the agent reaches knowledge over MCP (ADR-2)
        kg_tools = await ctx.kg.tools() if use_tools and hasattr(ctx.kg, "tools") else []  # type: ignore[attr-defined]
        gaps, rejected = await self._decisions(ctx)
        overrides, outcomes = await translate_with_ai(loaded.app, ctx.gateway, memory, overrides=overrides,
                                                      validator=validator, task=f"{ctx.run_id}:translate",
                                                      tools=use_tools, kg_tools=kg_tools, accepted_gaps=gaps,
                                                      rejected=rejected)
        if validator is not None and ctx.gateway is not None:
            # agent 26's diagnosis, run here so corrected SQL flows to synthesis, checks and the pack: tables
            # the rules translated but the data contradicts get one statement corrected (diagnose_mismatch)
            overrides, fixes = await repair_with_ai(loaded.app, ctx.gateway, memory, validator,
                                                    overrides=overrides, task=f"{ctx.run_id}:diagnose",
                                                    tools=use_tools, kg_tools=kg_tools)
            outcomes += fixes
        for o in outcomes:
            ai_outcomes.append({"statement": o.statement, "table": o.table, "status": o.status,
                                "attempts": len(o.attempts), "model_calls": o.model_calls,
                                "tool_calls": o.tool_calls, "notes": o.notes})
            if o.sql and o.status != "memory":
                ai_notes[o.statement] = f"{o.status}: {o.notes}"
        for e in memory.entries:
            if id(e) not in known:
                await self._remember(ctx, result, e)
        await self._record_checks(ctx, result, memory)
        if overrides:
            t = compose(loaded.app, overrides)
        key = put_json(ctx.artifacts, content_key(f"apps/{ctx.app_id}/translation",
                                                  repr(sorted(overrides.items())).encode() + job["bi_ir_key"].encode(),
                                                  ".json"),
                       {"bi_ir_key": job["bi_ir_key"], "overrides": {str(k): v for k, v in overrides.items()},
                        "ai_notes": {str(k): v for k, v in ai_notes.items()}, "ai_outcomes": ai_outcomes})
        result.artifacts.append(key)
        for name, r in t.tables.items():
            if r.mapping and not r.steps:
                continue
            ctes = [c for c in t.ctes.values() if any(s in c.spans for st in r.steps for s in st.spans)]
            evidence = [Evidence(kind="script_span", ref=f"{ctx.app_id}/{s}") for st in r.steps for s in st.spans] or \
                [Evidence(kind="script_span", ref=f"{ctx.app_id}/{u.span}") for u in r.untranslated]
            if not evidence:
                continue
            await self.propose(ctx, Fact(
                kind="bi_transform", subject=table_key(ctx.app_id, name), trust=Trust.INFERRED, evidence=evidence,
                value={"final_cte": r.final_cte, "ctes": [c.name for c in ctes],
                       "ai_assisted": [c.name for c in ctes if c.ai],
                       "untranslated": [{"statement": u.statement, "reason": u.reason, "tier": u.tier, "span": u.span,
                                         "text": u.text[:4000], "input_columns": u.input_columns}
                                        for u in r.untranslated],
                       "inputs_to_resolve": r.inputs, "notes": r.notes}), result)
        total = sum(len(r.steps) + len(r.untranslated) for r in t.tables.values())
        failed = sum(len(r.untranslated) for r in t.tables.values())
        result.outputs.update({"translation_key": key})
        result.metrics.update({"steps": total, "untranslated": failed, "ai_overrides": len(overrides),
                               "translated_ratio": round((total - failed) / total, 4) if total else 1.0})
        result.events.append("bi_transform.ready")
        return result

    async def _memory(self, ctx: AgentContext) -> TranslationMemory:
        """The translation memory from knowledge facts: the latest entry per statement, without disproved ones,
        marked proven when its re-checks (memory_check facts) passed on two or more apps and never failed."""
        latest: dict[str, Any] = {}
        for f in await ctx.kg.facts(kind="translation_memory"):
            latest[f.subject] = f
        stats = memory_stats(await ctx.kg.facts(kind="memory_check"))
        mem = TranslationMemory()
        for subject, f in latest.items():
            if f.value.get("status") != "disproved":
                e = MemoryEntry(**{k: f.value[k] for k in ("statement", "input_columns", "sql", "status", "app_id",
                                                           "replaces_rule") if k in f.value},
                                learned_at=f.recorded_at.isoformat())
                st = stats.get(subject.removeprefix("memory:"), {})
                confirmed = set(st.get("passed_apps", ())) | ({e.app_id} if e.status == "ai_validated" else set())
                e.proven = len(confirmed) >= 2 and not st.get("failed_apps")
                mem.entries.append(e)
        # SQL reviewers wrote for a statement, in any application: reused for the same statement elsewhere (checked
        # on that application's data like any memory answer) and shown as examples for similar statements
        for f in await ctx.kg.facts(kind="translation_override"):
            v = f.value
            if f.trust != Trust.CONFIRMED or not v.get("text"):
                continue
            key = f"memory:{memory_key(v['text'], v.get('input_columns'))}"
            if key in latest and latest[key].value.get("status") == "disproved":
                continue
            mem.add(MemoryEntry(statement=v["text"], input_columns=v.get("input_columns"), sql=v["sql"],
                                status="reviewer", app_id=f.subject.removeprefix("app:").split("/")[0],
                                learned_at=f.recorded_at.isoformat()))
        return mem

    async def _decisions(self, ctx: AgentContext) -> tuple[set[str], dict[str, dict[str, str]]]:
        """This application's reviewer decisions the AI loop acts on: statements accepted as untranslated (spans)
        and tables whose SQL a reviewer rejected (table -> reason and time)."""
        prefix = f"app:{ctx.app_id}/"
        gaps = {str(f.value.get("span")) for f in await ctx.kg.facts(kind="bi_gap_accepted")
                if f.trust == Trust.CONFIRMED and f.subject.startswith(prefix)}
        rejected = {f.subject.split("/table:")[-1]: {"reason": str(f.value.get("reason", "")),
                                                    "at": f.recorded_at.isoformat()}
                    for f in await ctx.kg.facts(kind="report_sql")
                    if f.status == FactStatus.REJECTED and f.subject.startswith(prefix)}
        return gaps, rejected

    async def _record_checks(self, ctx: AgentContext, result: AgentResult, memory: TranslationMemory) -> None:
        """Every reuse of a learned answer was checked on this app's data: keep the result, and retire an answer
        the data disproved (the latest translation_memory fact for it says disproved)."""
        for c in memory.checks:
            await self.propose(ctx, Fact(
                kind="memory_check", subject=f"memory:{c['key']}", trust=Trust.VALIDATED if c["passed"] and
                c["level"] in ("V3", "V4", "V5") else Trust.INFERRED,
                value={"app_id": ctx.app_id, "run_id": ctx.run_id, "passed": c["passed"], "level": c["level"],
                       "problems": c["problems"]},
                evidence=[Evidence(kind="query_result", ref=f"{ctx.app_id}:{ctx.run_id}")]), result)
        for e in memory.disproved:
            await self.propose(ctx, Fact(
                kind="translation_memory", subject=f"memory:{memory_key(e.statement, e.input_columns)}",
                trust=Trust.INFERRED, value={"statement": e.statement, "input_columns": e.input_columns,
                                             "sql": e.sql, "status": "disproved", "app_id": ctx.app_id,
                                             "replaces_rule": e.replaces_rule},
                evidence=[Evidence(kind="query_result", ref=f"{ctx.app_id}:{ctx.run_id}")]), result)

    async def _remember(self, ctx: AgentContext, result: AgentResult, e: MemoryEntry) -> None:
        await self.propose(ctx, Fact(
            kind="translation_memory", subject=f"memory:{memory_key(e.statement, e.input_columns)}",
            trust=Trust.VALIDATED if e.status == "ai_validated" else Trust.INFERRED,
            value={"statement": e.statement, "input_columns": e.input_columns, "sql": e.sql, "status": e.status,
                   "app_id": e.app_id, "replaces_rule": e.replaces_rule},
            evidence=[Evidence(kind="query_result" if e.status == "ai_validated" else "rule_check",
                               ref=f"{ctx.app_id}:{ctx.run_id}")]), result)
