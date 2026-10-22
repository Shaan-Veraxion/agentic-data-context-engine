"""Agent 28: BI script translator. Load-script statements -> Dremio SQL (CTE chains).

Rules first (adce_reversesql.compose); reviewer SQL next; then the LLM pathway (adce_reversesql.ai_loop): translation
memory (learned and reviewer answers, also used with AI off), then the model gateway route translate_statement with
the statement text and its input columns only (never rows). Every proposal is checked by code and, when the
uploads hold data (reload log, QVDs, source extracts), validated locally; failures go back to the model with the
problems found. Accepted translations are stored as translation_memory facts and reused by later runs of any app.
AI-assisted SQL stays marked for review. Reviewer decisions steer it too (adce_reversesql.ai_loop, ADR-5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adce_core.agent import (
    READ_TOOLS,
    WRITE_TOOLS,
    AgentBudget,
    AgentContext,
    AgentKind,
    AgentResult,
    AgentSpec,
    BaseAgent,
)
from adce_core.bi_ir import BIApp
from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_reversesql.ai_loop import Outcome, TranslationMemory, Validator, explain_gaps, translate_with_ai
from adce_reversesql.compose import compose
from adce_reversesql.learned import load_memory, record_memory
from adce_reversesql.runtime import load_translation, outcome_record, save_translation, validator_from_files

from .bi_extractor import table_key


@dataclass
class TranslateWork:
    """What the AI steps of one translation need. Built by prepare(); lives in memory for the run (the graph's
    checkpointed state keeps only the overrides, outcomes and queue; a resumed run builds this again)."""

    app: BIApp
    overrides: dict[int, str]  # reviewer SQL for this application, by statement
    ai_notes: dict[int, str]
    memory: TranslationMemory
    known: set[int]  # ids of the memory entries loaded at the start (the rest are learned in this run)
    validator: Validator | None
    use_tools: bool
    kg_tools: list[Any]
    accepted_gaps: set[str]
    rejected: dict[str, dict[str, str]]


class ScriptTranslator(BaseAgent):
    spec = AgentSpec(number=28, name="bi_script_translator", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_ir.ready"], emits=["bi_transform.ready"],
                     writes=["bi_transform", "translation_memory", "memory_check", "gap_analysis"],
                     routes=["translate_statement", "explain_gap"], subgroup="bi_adapters", hitl_modes=["queue"],
                     tools=[*READ_TOOLS, *WRITE_TOOLS, "get_feedback_history", "model_tools"],
                     budget=AgentBudget(max_calls=200))

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """The whole translation in one call (the bench, the command line). The orchestrator runs the same steps as
        graph nodes instead: prepare, then one node per statement step (ai_loop), then finish."""
        w = await self.prepare(job, ctx)
        overrides, outcomes = await translate_with_ai(w.app, ctx.gateway, w.memory, overrides=w.overrides,
                                                      validator=w.validator, task=f"{ctx.run_id}:translate",
                                                      tools=w.use_tools, kg_tools=w.kg_tools,
                                                      accepted_gaps=w.accepted_gaps, rejected=w.rejected)
        return await self.finish(job, ctx, w, overrides, outcomes)

    async def prepare(self, job: dict[str, Any], ctx: AgentContext) -> TranslateWork:
        """Rules, then the SQL reviewers wrote for this application, then what the AI steps need: the memory
        (learned and reviewer answers, used with AI off too), the validator over the uploads, the reviewer
        decisions, and the knowledge tools when tools are on."""
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"])
        overrides: dict[int, str] = {}
        ai_notes: dict[int, str] = {}
        reviewed = {f.value.get("span"): f.value["sql"] for f in await ctx.kg.facts(kind="translation_override")
                    if f.trust == Trust.CONFIRMED and f.subject.startswith(f"app:{ctx.app_id}/")}
        for r in loaded.translation.tables.values():
            for u in r.untranslated:
                if u.span in reviewed:
                    overrides[u.statement] = reviewed[u.span]
                    ai_notes[u.statement] = "reviewer-provided SQL"
        for qt in loaded.app.tables.values():  # Correct mode: reviewer SQL for statements the rules translated too
            for step in qt.steps:
                for op in step.ops:
                    span = f"{op.span.file}:{op.span.start_line}-{op.span.end_line}"
                    if span in reviewed and op.statement not in overrides:
                        overrides[op.statement] = reviewed[span]
                        ai_notes[op.statement] = "reviewer-corrected SQL"
        memory = await load_memory(ctx.kg)
        validator = validator_from_files(ctx.artifacts, job["files"], loaded.app) if job.get("files") else None
        use_tools = bool(job.get("llm_tools")) and ctx.gateway is not None
        # read-only knowledge tools come from kg-mcp when the agent reaches knowledge over MCP (ADR-2)
        kg_tools = await ctx.kg.tools() if use_tools and hasattr(ctx.kg, "tools") else []  # type: ignore[attr-defined]
        gaps, rejected = await self._decisions(ctx)
        return TranslateWork(loaded.app, overrides, ai_notes, memory, {id(e) for e in memory.entries}, validator,
                             use_tools, kg_tools, gaps, rejected)

    async def finish(self, job: dict[str, Any], ctx: AgentContext, w: TranslateWork, overrides: dict[int, str],
                     outcomes: list[Outcome]) -> AgentResult:
        """Record what was learned, explain what stays for a person (explain_gap), write the translation artifact
        and one bi_transform fact per table."""
        result = AgentResult()
        ai_notes = dict(w.ai_notes)
        for o in outcomes:
            if o.sql and o.status != "memory":
                ai_notes[o.statement] = f"{o.status}: {o.notes}"

        async def propose(f: Fact) -> str:
            return await self.propose(ctx, f, result)

        await record_memory(propose, w.memory, w.known, ctx.app_id, ctx.run_id)
        t = compose(w.app, overrides)
        analyses: list[dict[str, Any]] = []
        if ctx.gateway is not None:
            # what stays for a person: the model explains each gap and asks the data owner what it needs
            scripts = {f["name"]: ctx.artifacts.get(f["key"]).decode("utf-8-sig")
                       for f in job.get("files", []) if f["class"] == "script"}
            answers = {f.subject: f.value for f in await ctx.kg.facts(kind="gap_answer")
                       if f.trust == Trust.CONFIRMED and f.subject.startswith(f"app:{ctx.app_id}/")}
            analyses = await explain_gaps(w.app, t, ctx.gateway, scripts, outcomes, task=f"{ctx.run_id}:gaps",
                                          answers={k.removeprefix(f"app:{ctx.app_id}/gap:"): v
                                                   for k, v in answers.items()})
            for g in analyses:
                if "error" in g:
                    continue
                file, _, line = g["span"].rpartition(":")
                await self.propose(ctx, Fact(
                    kind="gap_analysis", subject=f"app:{ctx.app_id}/gap:{g['span']}", trust=Trust.INFERRED,
                    confidence={"low": 0.3, "medium": 0.6, "high": 0.8}.get(g["confidence"], 0.5),
                    value={k: v for k, v in g.items() if k not in ("model", "prompt_version")},
                    model=g.get("model") or None, prompt_version=g.get("prompt_version"),
                    evidence=[Evidence(kind="script_span", ref=f"{ctx.app_id}/{file}:{line}")]), result)
        key = save_translation(ctx.artifacts, ctx.app_id, job["bi_ir_key"], overrides, ai_notes,
                               [outcome_record(o) for o in outcomes], analyses)
        result.artifacts.append(key)
        for name, r in t.tables.items():
            if r.mapping and not r.steps:
                continue
            ctes = [c for c in t.ctes.values() if any(s in c.spans for st in r.steps for s in st.spans)]
            evidence = ([Evidence(kind="script_span", ref=f"{ctx.app_id}/{s}") for st in r.steps for s in st.spans]
                        or [Evidence(kind="script_span", ref=f"{ctx.app_id}/{u.span}") for u in r.untranslated])
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
                               "gap_analyses": sum("error" not in g for g in analyses),
                               "translated_ratio": round((total - failed) / total, 4) if total else 1.0})
        result.events.append("bi_transform.ready")
        return result

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
