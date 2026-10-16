"""Agent 27: BI extractor. Qlik scripts, reload logs and QVD headers -> BI IR (artifact) + documented facts.

Deterministic. Never writes to Qlik. Everything it records is 'documented' trust with the script span, reload-log
line or QVD header as evidence. Resolution of logical sources to Dremio datasets happens later (phase PD).
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import content_key, put_json
from adce_core.bi_ir import BIApp, LogicalSource, QlikTable, ScriptDocument, SourceSpan
from adce_core.facts import Entity, Evidence, Fact, Trust
from adce_qlik import parse_qvd_header, parse_reload_log, read_script
from adce_qlik.frontend import attach_frontend, attach_prj
from adce_qlik.model import build_app
from adce_qlik.qvd import QvdHeader
from adce_qlik.reload_log import ReloadLog


def table_key(app_id: str, name: str) -> str:
    return f"app:{app_id}/table:{name}"


def source_key(src: LogicalSource) -> str:
    return f"qvd:{src.path}" if src.kind == "qvd" else f"source:{src.key}"


def span_ref(app_id: str, span: SourceSpan) -> str:
    return f"{app_id}/{span.file}:{span.start_line}-{span.end_line}"


class BIExtractor(BaseAgent):
    spec = AgentSpec(
        number=27, name="bi_extractor", version="0.1.0", kind=AgentKind.DETERMINISTIC,
        triggers=["upload.classified[bi_app]", "bi_inputs.received"], emits=["bi_ir.ready"],
        writes=["bi_table", "contains", "reads_source", "stores_qvd", "reads_qvd", "uses_mapping", "variable_value",
                "loaded_rows", "qvd_header", "bi_coverage", "bi_issue", "bi_measure", "bi_visual",
                "rls_requirement"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """job: {"files": [{"name", "key", "class"}]} as produced by the intake router."""
        result = AgentResult()
        files = job["files"]
        scripts = {f["name"]: ctx.artifacts.get(f["key"]).decode("utf-8-sig")
                   for f in files if f["class"] == "script"}
        if not scripts:
            result.metrics["error"] = "no script files"
            return result
        log: ReloadLog | None = None
        logs = [f for f in files if f["class"] == "reload_log"]
        if logs:
            log = parse_reload_log(ctx.artifacts.get(logs[-1]["key"]).decode("utf-8", errors="replace"))
        headers: dict[str, QvdHeader] = {}
        for f in files:
            if f["class"] == "qvd":
                name = PurePosixPath(f["name"].replace("\\", "/")).name
                headers[name] = parse_qvd_header(ctx.artifacts.get(f["key"]))

        seed = {f.subject.rsplit("/var:", 1)[-1]: str(f.value["value"])
                for f in await ctx.kg.facts(kind="variable_value")
                if f.subject.startswith(f"app:{ctx.app_id}/var:") and f.trust == Trust.CONFIRMED}
        doc = read_script(ctx.app_id, scripts, log, seed_variables=seed)
        app = build_app(doc, qvd_columns={n: [fl.name for fl in h.fields] for n, h in headers.items()})
        objects = {f["name"]: ctx.artifacts.get(f["key"]).decode("utf-8-sig") for f in files
                   if f["class"] == "app_objects"}
        if objects:
            attach_frontend(app, objects)
        prj = {f["name"]: ctx.artifacts.get(f["key"]).decode("utf-8-sig", errors="replace") for f in files
               if f["class"] == "qlikview_project"}
        if prj:  # QlikView -prj folder: charts and expressions (the script itself is LoadScript.txt, a script file)
            attach_prj(app, prj)

        ir = {"document": doc.model_dump(mode="json"), "app": app.model_dump(mode="json")}
        payload = put_json(ctx.artifacts, content_key(f"apps/{ctx.app_id}/bi_ir", repr(ir).encode(), ".json"), ir)
        result.artifacts.append(payload)

        await self._record(ctx, result, doc, app, log, headers, logs[-1]["key"] if logs else None)
        result.outputs.update({"bi_ir_key": payload, "tables": [t.name for t in app.tables.values()],
                               "sources": [s.key for s in app.sources], "visuals": [v.id for v in app.visuals]})
        result.metrics.update({"statements": app.coverage.total, "coverage": round(app.coverage.ratio, 4),
                               "tables": len(app.tables), "issues": len(app.issues)})
        result.events.append("bi_ir.ready")
        return result

    # ------------------------------------------------------------------ facts
    async def _record(self, ctx: AgentContext, result: AgentResult, doc: ScriptDocument, app: BIApp,
                      log: ReloadLog | None, headers: dict[str, QvdHeader], log_key: str | None) -> None:
        app_key = f"app:{ctx.app_id}"
        a = ctx.app_id

        def ev(span: SourceSpan) -> list[Evidence]:
            return [Evidence(kind="script_span", ref=span_ref(a, span))]

        for src in app.sources:
            await self.register(ctx, Entity(key=source_key(src), type="QvdFile" if src.kind == "qvd" else
                                            "LogicalSource", name=src.path,
                                            attributes={"connection": src.connection, "kind": src.kind,
                                                        "resolution": "pending_dremio"}))
        for t in app.tables.values():
            tk = table_key(a, t.name)
            first = t.steps[0].ops[0].span
            await self.register(ctx, Entity(key=tk, type="QlikTable", name=t.name,
                                            attributes={"app": app_key, "mapping": t.mapping}))
            await self.propose(ctx, Fact(kind="contains", subject=app_key, object=tk, value={"table": t.name},
                                         trust=Trust.DOCUMENTED, evidence=ev(first)), result)
            await self.propose(ctx, Fact(
                kind="bi_table", subject=tk, trust=Trust.DOCUMENTED, evidence=ev(first),
                value={"fields": t.fields, "mapping": t.mapping, "dropped": t.dropped_at is not None,
                       "name_inferred": t.name_inferred,
                       "steps": [{"action": s.action.value, "join_kind": s.join_kind, "other_table": s.other_table,
                                  "statements": [o.statement for o in s.ops]} for s in t.steps]}), result)
            for step in t.steps:
                for op in step.ops:
                    await self._source_links(ctx, result, app, t, op.source.kind, op, ev(op.span))
            for mapping, op in _mappings_used(t):
                await self.propose(ctx, Fact(kind="uses_mapping", subject=tk, object=table_key(a, mapping),
                                             value={"via": "applymap", "statement": op.statement},
                                             trust=Trust.DOCUMENTED, evidence=ev(op.span)), result)
            for target in t.stored_to:
                await self.propose(ctx, Fact(kind="stores_qvd", subject=tk, object=f"qvd:{target}",
                                             value={"path": target}, trust=Trust.DOCUMENTED,
                                             evidence=ev(first)), result)
            if log is not None:
                await self._row_counts(ctx, result, t, tk, log, log_key)
        for v in doc.variables.values():
            if v.value is None:
                continue
            evidence = ([Evidence(kind="reload_log", ref=log_key or "reload_log", detail={"variable": v.name})]
                        if v.origin.value == "reload_log" else ev(v.span) if v.span else [])
            if evidence:
                await self.propose(ctx, Fact(kind="variable_value", subject=f"{app_key}/var:{v.name}",
                                             value={"value": v.value, "origin": v.origin.value},
                                             trust=Trust.DOCUMENTED, evidence=evidence), result)
        for m in app.measures:
            await self.propose(ctx, Fact(kind="bi_measure", subject=f"{app_key}/measure:{m.id}",
                                         value={"label": m.label, "expression": m.expression},
                                         trust=Trust.DOCUMENTED,
                                         evidence=[Evidence(kind="app_object", ref=f"{a}/measure:{m.id}")]), result)
        for v in app.visuals:
            await self.register(ctx, Entity(key=f"{app_key}/visual:{v.id}", type="Visual", name=v.title or v.id,
                                            attributes={"app": app_key, "type": v.type, "sheet": v.sheet}))
            await self.propose(ctx, Fact(
                kind="bi_visual", subject=f"{app_key}/visual:{v.id}", trust=Trust.DOCUMENTED,
                value={"type": v.type, "title": v.title, "sheet": v.sheet,
                       "dimensions": [{"label": d.label, "field": d.field} for d in v.dimensions],
                       "measures": [{"label": x.label, "expression": x.expression} for x in v.measures]},
                evidence=[Evidence(kind="app_object", ref=f"{a}/visual:{v.id}")]), result)
        for name, h in headers.items():
            await self.propose(ctx, Fact(
                kind="qvd_header", subject=f"qvd:{_match_store(app, name) or name}", trust=Trust.DOCUMENTED,
                value={"table": h.table_name, "records": h.records, "fields": [f.name for f in h.fields],
                       "creator_doc": h.creator_doc, "created_utc": h.created_utc},
                evidence=[Evidence(kind="qvd_header", ref=name)]), result)
        await self.propose(ctx, Fact(kind="bi_coverage", subject=app_key, trust=Trust.DOCUMENTED,
                                     value=app.coverage.model_dump() | {"ratio": round(app.coverage.ratio, 4)},
                                     evidence=[Evidence(kind="script_span", ref=f"{a}/{f}") for f in doc.files]),
                           result)
        for at in app.section_access:  # Section Access: a row-level security requirement, never translated
            await self.propose(ctx, Fact(kind="rls_requirement", subject=f"{app_key}/access:{at.name}",
                                         trust=Trust.DOCUMENTED,
                                         value={"table": at.name, "fields": at.fields,
                                                "reduction_fields": at.reduction_fields},
                                         evidence=ev(at.span) if at.span else
                                         [Evidence(kind="script_span", ref=f"{a}/script")]), result)
        for i in app.issues:
            await self.propose(ctx, Fact(kind="bi_issue", subject=app_key, trust=Trust.DOCUMENTED,
                                         value={"code": i.code, "message": i.message, "statement": i.statement},
                                         evidence=ev(i.span) if i.span else
                                         [Evidence(kind="script_span", ref=f"{a}/script")]), result)

    async def _source_links(self, ctx: AgentContext, result: AgentResult, app: BIApp, t: QlikTable, kind: str,
                            op: Any, evidence: list[Evidence]) -> None:
        tk = table_key(app.app_id, t.name)
        if kind == "sql":
            for path in op.source.tables:
                src = LogicalSource(connection=op.source.connection, path=path, kind="sql_table")
                await self.propose(ctx, Fact(kind="reads_source", subject=tk, object=source_key(src),
                                             value={"via": "sql", "statement": op.statement}, trust=Trust.DOCUMENTED,
                                             evidence=evidence), result)
        elif kind in ("qvd", "file") and op.source.ref:
            fact_kind = "reads_qvd" if kind == "qvd" else "reads_source"
            obj = f"qvd:{op.source.ref}" if kind == "qvd" else f"source::{op.source.ref}"
            await self.propose(ctx, Fact(kind=fact_kind, subject=tk, object=obj,
                                         value={"via": kind, "statement": op.statement}, trust=Trust.DOCUMENTED,
                                         evidence=evidence), result)
        elif kind == "resident" and op.source.ref:
            await self.propose(ctx, Fact(kind="reads_source", subject=tk, object=table_key(app.app_id, op.source.ref),
                                         value={"via": "resident", "statement": op.statement},
                                         trust=Trust.DOCUMENTED, evidence=evidence), result)

    async def _row_counts(self, ctx: AgentContext, result: AgentResult, t: QlikTable, tk: str, log: ReloadLog,
                          log_key: str | None) -> None:
        for step in t.steps:
            for op in step.ops:
                for f in log.fetches:
                    if f.line_no is not None and op.span.start_line <= f.line_no <= op.span.end_line:
                        await self.propose(ctx, Fact(
                            kind="loaded_rows", subject=tk, trust=Trust.DOCUMENTED,
                            value={"lines_fetched": f.lines, "statement": op.statement, "action": step.action.value,
                                   "reload_time": f.timestamp},
                            evidence=[Evidence(kind="reload_log", ref=log_key or "reload_log",
                                               detail={"line_no": f.line_no, "timestamp": f.timestamp})]), result)


def _mappings_used(t: QlikTable) -> list[tuple[str, Any]]:
    """(mapping table, op) for every ApplyMap('Name', ...) with a literal map name in the table's loads."""
    out: list[tuple[str, Any]] = []

    def walk(e: Any, op: Any) -> None:
        if e is None:
            return
        if e.kind == "call" and (e.name or "").lower() == "applymap" and e.args and e.args[0].kind == "string":
            name = str(e.args[0].value)
            if name not in [m for m, _ in out]:
                out.append((name, op))
        for a in e.args:
            walk(a, op)

    for step in t.steps:
        for op in step.ops:
            for f in op.fields:
                walk(f.expr, op)
            walk(op.where, op)
    return out


def _match_store(app: BIApp, file_name: str) -> str | None:
    for s in app.stores:
        if PurePosixPath(s.target.replace("\\", "/")).name.lower() == file_name.lower():
            return s.target
    return None
