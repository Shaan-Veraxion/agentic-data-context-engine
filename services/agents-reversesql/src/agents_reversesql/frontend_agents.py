"""Front-end agents for the Qlik path: the data model behind the charts and the exported reports.

23 Join planner    associations between the final tables (shared field names), synthetic keys, circular references,
                   and the join tree each chart needs
21 Report parser   reads exported reports into tables of figures: Excel and CSV directly, PDF through the parse_export
                   model route (its text; the figures are aggregates); images and scans go to review
22 Metric mapper   matches each exported table to the chart that produced it, column by column; labels decide, the
                   map_metric AI route helps only when labels do not, and only with labels and expressions
"""
from __future__ import annotations

import json
import re
from typing import Any

from adce_core.agent import AgentBudget, AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import put_json
from adce_core.facts import Evidence, Fact, Trust
from adce_reversesql.charts import ChartTranslator, DataModel
from adce_reversesql.exports import (
    MAX_PDF_CHARS,
    ExportTable,
    ParsedExport,
    map_export,
    pdf_pages,
    read_export,
    tables_from_parsed,
)
from adce_reversesql.expressions import Untranslatable
from adce_reversesql.runtime import load_translation
from pydantic import BaseModel

from .agents import table_key


def visual_key(app_id: str, visual_id: str) -> str:
    return f"app:{app_id}/visual:{visual_id}"


def export_key(app_id: str, file: str, sheet: str) -> str:
    return f"app:{app_id}/export:{file}#{sheet}"


class JoinPlanner(BaseAgent):
    spec = AgentSpec(number=23, name="join_planner", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["measures.translated"], emits=["joins.planned"],
                     writes=["associates", "model_issue", "join_plan"], hitl_modes=["queue"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        model = DataModel(loaded.app, loaded.translation)
        a = ctx.app_id
        for x in model.associations:
            await self.propose(ctx, Fact(kind="associates", subject=table_key(a, x.a), object=table_key(a, x.b),
                                         value={"keys": x.keys, "synthetic_key": len(x.keys) > 1},
                                         trust=Trust.DOCUMENTED,
                                         evidence=[Evidence(kind="bi_ir", ref=job["bi_ir_key"])]), result)
        issues = [f"synthetic key {x.a}–{x.b}: {', '.join(x.keys)}" for x in model.synthetic_keys()]
        if model.has_cycle():
            issues.append("circular reference between tables")
        for msg in issues:
            await self.propose(ctx, Fact(kind="model_issue", subject=f"app:{a}", value={"message": msg},
                                         trust=Trust.DOCUMENTED,
                                         evidence=[Evidence(kind="bi_ir", ref=job["bi_ir_key"])]), result)
        if not loaded.app.visuals:  # the associations above are still recorded: they come from the script
            result.metrics["idle"] = ("no chart objects in the upload (Qlik Sense app objects JSON, e.g. from "
                                      "qlik app unbuild): only the data model's associations were recorded")
        tr = ChartTranslator(loaded.app, loaded.translation)
        planned = 0
        for v in loaded.app.visuals:
            value: dict[str, Any]
            try:
                roots = sorted({t for m in v.measures for t in tr.check_measure(m.expression)[3]})
                if not roots:
                    raise Untranslatable("no translatable measure", "T2")
                needed = sorted({model.nearest_owner(d.field, roots[0]) for d in v.dimensions
                                 if not d.field.startswith("=")} | set(roots[1:]))
                plan = model.plan(roots[0], [t for t in needed if t != roots[0]])
                value = {"root": plan.root, "tables": plan.tables,
                         "joins": [{"from": p, "to": c, "keys": k} for p, c, k in plan.edges]}
                planned += 1
            except Untranslatable as e:
                value = {"error": str(e), "tier": e.tier}
            await self.propose(ctx, Fact(kind="join_plan", subject=visual_key(a, v.id), value=value,
                                         trust=Trust.INFERRED,
                                         evidence=[Evidence(kind="app_object", ref=f"{a}/visual:{v.id}")]), result)
        result.metrics.update({"associations": len(model.associations), "model_issues": len(issues),
                               "visuals_planned": planned, "visuals": len(loaded.app.visuals)})
        result.events.append("joins.planned")
        return result


class ReportParser(BaseAgent):
    spec = AgentSpec(number=21, name="report_parser", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["upload.classified[bi_app]"], emits=["exports.parsed"],
                     writes=["report_export", "export_unreadable"], routes=["parse_export"], hitl_modes=["queue"],
                     budget=AgentBudget(max_calls=20))

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        parsed: list[dict[str, Any]] = []
        if not any(f["class"] == "report_export" for f in job.get("files", [])):
            result.metrics["idle"] = "no exported reports in the upload (xlsx / csv exports of Qlik charts)"
        for f in job.get("files", []):
            if f["class"] != "report_export":
                continue
            data = ctx.artifacts.get(f["key"])
            via = "file"
            try:
                if f["name"].lower().endswith(".pdf"):
                    tables, via = await self._pdf(ctx, f["name"], data), "model"
                else:
                    tables = read_export(f["name"], data)
            except ValueError as e:  # images, a PDF without a model or without text: a person reads it
                await self.propose(ctx, Fact(kind="export_unreadable", subject=f"app:{ctx.app_id}",
                                             value={"file": f["name"], "reason": str(e)}, trust=Trust.DOCUMENTED,
                                             evidence=[Evidence(kind="export", ref=f["key"])]), result)
                continue
            for t in tables:
                key = put_json(ctx.artifacts, f"apps/{ctx.app_id}/runs/{ctx.run_id}/exports/"
                               f"{_safe(f['name'])}.{_safe(t.sheet)}.json", t.__dict__)
                parsed.append({"file": f["name"], "sheet": t.sheet, "key": key, "via": via})
                await self.propose(ctx, Fact(
                    kind="report_export", subject=export_key(ctx.app_id, f["name"], t.sheet),
                    trust=Trust.DOCUMENTED if via == "file" else Trust.INFERRED,
                    value={"headers": t.headers, "rows": len(t.rows), "totals": t.totals is not None, "read_by": via},
                    evidence=[Evidence(kind="export", ref=f["key"], detail={"sheet": t.sheet})]), result)
        result.outputs["exports"] = parsed
        result.metrics.update({"tables": len(parsed), "read_by_model": sum(1 for p in parsed if p["via"] == "model")})
        result.events.append("exports.parsed")
        return result

    async def _pdf(self, ctx: AgentContext, name: str, data: bytes) -> list[ExportTable]:
        """A PDF export: its text, read into tables of figures by the route parse_export (the figures are
        aggregates, ADR-Q6). The model copies, never computes; a table whose rows do not fit its headers is
        dropped (exports.tables_from_parsed)."""
        from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss

        if ctx.gateway is None:
            raise ValueError("a PDF export is read by the parse_export model route; this run has no model")
        pages = pdf_pages(data)
        if not any(p.strip() for p in pages):
            raise ValueError("the PDF has no text (a scan or an image): a person enters its figures")
        text, used = [], 0
        for p in pages:  # keep within what the route is given
            text.append(p[: max(0, MAX_PDF_CHARS - used)])
            used += len(text[-1])
        try:
            r = await ctx.gateway.call("parse_export", {"file": name, "pages": text}, ParsedExport,
                                       task=f"{ctx.run_id}:export:{name}")
        except (ReplayMiss, GatewayRefusal, OutputInvalid, ProviderError, BudgetExceeded) as e:
            raise ValueError(f"the parse_export route could not read it: {type(e).__name__}") from e
        out = r.output
        assert isinstance(out, ParsedExport)
        tables = tables_from_parsed(name, out)
        if not tables:
            raise ValueError(f"the model found no table of figures it could line up: {out.notes or 'no notes'}")
        return tables


class MetricChoice(BaseModel):
    """Structured output of the model-gateway route map_metric."""

    measure_id: str | None
    confidence: str  # high | medium | low
    notes: str = ""


class MetricMapper(BaseAgent):
    spec = AgentSpec(number=22, name="metric_mapper", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["exports.parsed", "joins.planned"], emits=["metrics.mapped"],
                     writes=["export_maps_to", "export_unmapped"], routes=["map_metric"], hitl_modes=["ask"],
                     budget=AgentBudget(max_calls=40))

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        visuals = loaded.app.visuals
        mapped = 0
        if not job.get("exports"):
            result.metrics["idle"] = "no exported reports to match to charts (agent 21 found none)"
        for e in job.get("exports", []):
            et = ExportTable(**json.loads(ctx.artifacts.get(e["key"])))
            m = map_export(et, visuals)
            if m.visual_id is None and ctx.gateway is not None:
                m = await self._with_ai(ctx, et, visuals) or m
            subject = export_key(ctx.app_id, e["file"], e["sheet"])
            if m.visual_id is None:
                await self.propose(ctx, Fact(kind="export_unmapped", subject=subject, value={"reason": m.reason},
                                             trust=Trust.INFERRED,
                                             evidence=[Evidence(kind="export", ref=e["key"])]), result)
                continue
            mapped += 1
            v = next(x for x in visuals if x.id == m.visual_id)
            cols = {et.headers[i]: (v.dimensions[j].label if k == "dimension" else v.measures[j].label)
                    for i, (k, j) in m.columns.items()}
            await self.propose(ctx, Fact(kind="export_maps_to", subject=subject, object=visual_key(ctx.app_id, v.id),
                                         value={"columns": cols, "confidence": m.confidence, "by": m.reason},
                                         trust=Trust.INFERRED if m.confidence != "high" else Trust.DOCUMENTED,
                                         evidence=[Evidence(kind="export", ref=e["key"])]), result)
        result.metrics.update({"exports": len(job.get("exports", [])), "mapped": mapped})
        result.events.append("metrics.mapped")
        return result

    async def _with_ai(self, ctx: AgentContext, et: ExportTable, visuals: list[Any]) -> Any | None:
        """Ask map_metric for each header no visual label matches; then retry the label match with its answers."""
        from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss

        gateway = ctx.gateway
        assert gateway is not None
        candidates = [{"measure_id": f"{v.id}|{m.id}", "label": m.label, "expression": m.expression,
                       "chart": v.title} for v in visuals for m in v.measures]
        renamed = list(et.headers)
        labels = {f"{v.id}|{m.id}": m.label for v in visuals for m in v.measures}
        known = {x.label.strip().lower() for v in visuals for x in [*v.dimensions, *v.measures]}
        for i, h in enumerate(et.headers):
            if h.strip().lower() in known:
                continue
            try:
                res = await gateway.call("map_metric", {"column_header": h, "report": et.file, "sheet": et.sheet,
                                                            "other_headers": et.headers, "candidates": candidates},
                                             MetricChoice, task=f"{ctx.run_id}:map")
            except (ReplayMiss, GatewayRefusal, OutputInvalid, ProviderError, BudgetExceeded):
                return None
            out = res.output
            assert isinstance(out, MetricChoice)
            if out.measure_id is None or out.measure_id not in labels or out.confidence == "low":
                return None
            renamed[i] = labels[out.measure_id]
        m = map_export(ExportTable(et.file, et.sheet, renamed, et.rows, et.totals), visuals)
        if m.visual_id is None:
            return None
        m.export, m.confidence, m.reason = et, "medium", "labels with AI-matched headers (needs review)"
        return m


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s)
