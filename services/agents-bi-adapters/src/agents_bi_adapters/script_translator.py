"""Agent 28: BI script translator. Load-script statements -> Dremio SQL (CTE chains).

Rules first (adce_reversesql.compose); reviewer SQL next; then the LLM pathway (adce_reversesql.ai_loop): translation
memory, then the model gateway route translate_statement with the statement text and its input columns only (never
rows). Every proposal is checked by code and, when the uploads hold data (reload log, QVDs, source extracts),
validated locally; failures go back to the model with the problems found. Accepted translations are stored as
translation_memory facts and reused by later runs of any app. AI-assisted SQL stays marked for review.
"""
from __future__ import annotations

import hashlib
from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import content_key, put_json
from adce_core.facts import Evidence, Fact, Trust
from adce_reversesql.ai_loop import MemoryEntry, TranslationMemory, normalise, repair_with_ai, translate_with_ai
from adce_reversesql.compose import compose
from adce_reversesql.runtime import load_translation, validator_from_files

from .bi_extractor import table_key


class ScriptTranslator(BaseAgent):
    spec = AgentSpec(number=28, name="bi_script_translator", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_ir.ready"], emits=["bi_transform.ready"],
                     writes=["bi_transform", "translation_memory"], routes=["translate_statement", "diagnose_mismatch"])

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
        if ctx.gateway is not None:
            memory = await self._memory(ctx)
            known = {id(e) for e in memory.entries}
            validator = validator_from_files(ctx.artifacts, job["files"], loaded.app) if job.get("files") else None
            overrides, outcomes = await translate_with_ai(loaded.app, ctx.gateway, memory, overrides=overrides,
                                                          validator=validator, task=f"{ctx.run_id}:translate")
            if validator is not None:
                # agent 26's diagnosis, run here so corrected SQL flows to synthesis, checks and the pack: tables
                # the rules translated but the data contradicts get one statement corrected (diagnose_mismatch)
                overrides, fixes = await repair_with_ai(loaded.app, ctx.gateway, memory, validator,
                                                        overrides=overrides, task=f"{ctx.run_id}:diagnose")
                outcomes += fixes
            for o in outcomes:
                ai_outcomes.append({"statement": o.statement, "table": o.table, "status": o.status,
                                    "attempts": len(o.attempts), "notes": o.notes})
                if o.sql and o.status != "memory":
                    ai_notes[o.statement] = f"{o.status}: {o.notes}"
            for e in memory.entries:
                if id(e) not in known:
                    await self._remember(ctx, result, e)
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
                       "untranslated": [{"statement": u.statement, "reason": u.reason, "tier": u.tier, "span": u.span}
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
        """The translation memory from knowledge facts: the latest entry per statement, without disproved ones."""
        latest: dict[str, Any] = {}
        for f in await ctx.kg.facts(kind="translation_memory"):
            latest[f.subject] = f
        mem = TranslationMemory()
        for f in latest.values():
            if f.value.get("status") != "disproved":
                mem.entries.append(MemoryEntry(**{k: f.value[k] for k in ("statement", "input_columns", "sql",
                                                                          "status", "app_id", "replaces_rule")
                                                  if k in f.value}))
        return mem

    async def _remember(self, ctx: AgentContext, result: AgentResult, e: MemoryEntry) -> None:
        key = hashlib.sha256((normalise(e.statement) + "|" + ",".join(e.input_columns or [])).encode()).hexdigest()
        await self.propose(ctx, Fact(
            kind="translation_memory", subject=f"memory:{key[:24]}", trust=Trust.VALIDATED
            if e.status == "ai_validated" else Trust.INFERRED,
            value={"statement": e.statement, "input_columns": e.input_columns, "sql": e.sql, "status": e.status,
                   "app_id": e.app_id, "replaces_rule": e.replaces_rule},
            evidence=[Evidence(kind="query_result" if e.status == "ai_validated" else "rule_check",
                               ref=f"{ctx.app_id}:{ctx.run_id}")]), result)
