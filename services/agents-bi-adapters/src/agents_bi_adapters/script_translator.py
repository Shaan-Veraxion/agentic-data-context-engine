"""Agent 28: BI script translator. Load-script statements -> Dremio SQL (CTE chains).

Rules first (adce_reversesql.compose). Statements the rules cannot translate (tier T2/T3) go to the model-gateway
route translate_statement, with the statement text and its input columns only (never rows). An AI translation is
accepted only if it parses as Dremio SQL; it is marked AI-assisted and needs review before trust is raised.
"""
from __future__ import annotations

from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import content_key, put_json
from adce_core.facts import Evidence, Fact, Trust
from adce_reversesql.compose import compose
from adce_reversesql.runtime import StatementTranslation, load_translation

from .bi_extractor import table_key


class ScriptTranslator(BaseAgent):
    spec = AgentSpec(number=28, name="bi_script_translator", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_ir.ready"], emits=["bi_transform.ready"], writes=["bi_transform"],
                     routes=["translate_statement"])

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
        if ctx.gateway is not None:
            for r in t.tables.values():
                for u in r.untranslated:
                    if u.tier == "T1" or "depends on" in u.reason or u.statement in overrides:
                        continue
                    out = await self._ask(ctx, u.text, u.reason, u.input_columns)
                    if out is not None:
                        overrides[u.statement] = out.sql
                        ai_notes[u.statement] = out.notes
            if overrides:
                t = compose(loaded.app, overrides)
        key = put_json(ctx.artifacts, content_key(f"apps/{ctx.app_id}/translation",
                                                  repr(sorted(overrides.items())).encode() + job["bi_ir_key"].encode(),
                                                  ".json"),
                       {"bi_ir_key": job["bi_ir_key"], "overrides": {str(k): v for k, v in overrides.items()},
                        "ai_notes": {str(k): v for k, v in ai_notes.items()}})
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

    async def _ask(self, ctx: AgentContext, text: str, reason: str,
                   columns: list[str] | None) -> StatementTranslation | None:
        from adce_gateway.gateway import GatewayRefusal, OutputInvalid, ReplayMiss

        try:
            res = await ctx.gateway.call("translate_statement",  # type: ignore[union-attr]
                                         {"statement": text, "why_rules_failed": reason,
                                          "input_relation": "src", "input_columns": columns},
                                         StatementTranslation, task=f"{ctx.run_id}:translate")
        except (ReplayMiss, GatewayRefusal, OutputInvalid):
            return None
        out = res.output
        assert isinstance(out, StatementTranslation)
        return out if out.translatable and out.sql.strip() else None
