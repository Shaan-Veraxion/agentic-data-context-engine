"""Agent 29: expression translator. Chart measures (front-end expressions) -> SQL aggregations.

Hybrid. Rules first (adce_reversesql.charts): aggregations with Qlik's per-record semantics, set analysis as
filters, TOTAL, arithmetic between aggregations. A measure the rules cannot translate goes to the model gateway
route translate_expression; its answer is recorded as a suggestion for a reviewer, never applied silently. The
prompt carries the expression, field names and their tables only (no data).
"""
from __future__ import annotations

from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.facts import Evidence, Fact, Trust
from adce_reversesql.charts import ChartTranslator
from adce_reversesql.runtime import load_translation
from pydantic import BaseModel


class ExpressionSuggestion(BaseModel):
    """Structured output of the model-gateway route translate_expression."""

    sql: str  # Dremio SQL for the measure, over the tables named in the input
    translatable: bool
    notes: str = ""


def measure_key(app_id: str, visual_id: str, measure_id: str) -> str:
    return f"app:{app_id}/visual:{visual_id}/measure:{measure_id}"


class ExpressionTranslator(BaseAgent):
    spec = AgentSpec(number=29, name="bi_expression_translator", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_transform.ready"], emits=["measures.translated"], writes=["measure_translation"],
                     routes=["translate_expression"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        tr = ChartTranslator(loaded.app, loaded.translation)
        total = ok_count = suggested = 0
        if not loaded.app.visuals:
            result.metrics["idle"] = ("no chart objects in the upload (Qlik Sense app objects JSON, e.g. from "
                                      "qlik app unbuild)")
        for v in loaded.app.visuals:
            dims = [d.field for d in v.dimensions]
            for m in v.measures:
                total += 1
                ok, reason, tier, tables = tr.check_measure(m.expression)
                value: dict[str, Any] = {"label": m.label, "expression": m.expression, "tables": tables}
                trust = Trust.INFERRED
                if ok:
                    ok_count += 1
                    value["status"] = "translated"
                else:
                    value.update({"status": "untranslated", "reason": reason, "tier": tier})
                    s = await self._suggest(ctx, m.expression, reason, dims, tr)
                    if s is not None:
                        suggested += 1
                        value.update({"status": "ai_suggestion", "suggested_sql": s.sql, "notes": s.notes})
                await self.propose(ctx, Fact(kind="measure_translation", subject=measure_key(ctx.app_id, v.id, m.id),
                                             value=value, trust=trust,
                                             evidence=[Evidence(kind="app_object", ref=f"{ctx.app_id}/visual:{v.id}")]),
                                   result)
        result.metrics.update({"measures": total, "translated": ok_count, "ai_suggestions": suggested,
                               "translated_ratio": round(ok_count / total, 4) if total else 1.0})
        result.events.append("measures.translated")
        return result

    async def _suggest(self, ctx: AgentContext, expression: str, reason: str, dims: list[str],
                       tr: ChartTranslator) -> ExpressionSuggestion | None:
        if ctx.gateway is None:
            return None
        from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss

        fields = [{"field": f, "tables": tr.model.owners(f)} for t in tr.model.fields.values() for f in t]
        uniq = list({x["field"]: x for x in fields}.values())
        try:
            res = await ctx.gateway.call("translate_expression",
                                         {"expression": expression, "why_rules_failed": reason, "dimensions": dims,
                                          "fields": uniq, "tables": list(tr.model.fields)},
                                         ExpressionSuggestion, task=f"{ctx.run_id}:measures")
        except (ReplayMiss, GatewayRefusal, OutputInvalid, ProviderError, BudgetExceeded):
            return None
        out = res.output
        assert isinstance(out, ExpressionSuggestion)
        return out if out.translatable and out.sql.strip() else None
