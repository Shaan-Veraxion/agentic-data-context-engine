"""Reverse-SQL agents for the Qlik path.

24 Synthesizer     renders the Dremio SQL per table (CTE chain with statement map) and per chart as artifacts
25 Query checker   runs the rule checks (syntax, translation, fields, determinism, inputs); with a model, repairs a
                   table that fails a blocking rule (route repair_sql, at most twice)
26 Reconciler      executes in the venue available (local DuckDB until phase PD) and climbs the validation ladder;
                   classifies each mismatch (adce_reversesql.diagnose) and, with a model, corrects the statement the
                   evidence contradicts (route diagnose_mismatch); charts with an exported report reach V5 when every
                   figure matches
19 Lineage (lite)  dataset-level lineage from logical sources through Qlik tables to QVDs, OpenLineage-shaped
20 Publisher (lite) writes the migration pack: SQL, lineage, validation report, review record, manifest
"""
from __future__ import annotations

import hashlib
import json
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
from adce_core.artifacts import put_json
from adce_core.facts import Evidence, Fact, Trust
from adce_reversesql.ai_loop import diagnose_table, failed_rules, repair_table
from adce_reversesql.charts import translate_charts
from adce_reversesql.check import check_table
from adce_reversesql.diagnose import classify
from adce_reversesql.figures import check_exports
from adce_reversesql.learned import load_memory, record_memory
from adce_reversesql.reconcile import Level, Reconciler
from adce_reversesql.resolve import apply_mappings, known_mappings
from adce_reversesql.runtime import (
    load_translation,
    local_evidence,
    outcome_record,
    save_translation,
    table_fields,
    translation_doc,
    validator_from_files,
)
from adce_reversesql.semantics import chart_rules, script_rules

from . import pack


def table_key(app_id: str, name: str) -> str:
    return f"app:{app_id}/table:{name}"


def output_tables(loaded: Any) -> list[str]:
    """Tables a pack covers: those left in the data model, and any table stored to a QVD."""
    t = loaded.translation
    out = []
    for name, r in t.tables.items():
        qt = loaded.app.tables.get(name)
        keep = qt is not None and not qt.mapping and (qt.dropped_at is None or r.stores)
        if keep:
            out.append(name)
    return out


class Synthesizer(BaseAgent):
    spec = AgentSpec(number=24, name="sql_synthesizer", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["bi_transform.ready", "joins.planned"], emits=["sql.candidates"],
                     writes=["sql_candidate", "chart_sql"], hitl_modes=[])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        sql_keys = {}
        mappings = await known_mappings(ctx.kg, job.get("source_map"))
        pending: dict[str, list[str]] = {}
        mapped_all: set[str] = set()
        for name in output_tables(loaded):
            sql = loaded.translation.table_sql(name)
            if sql is None:
                continue
            # the Dremio names of the sources (reviewer map decisions; until then the SQL says what is missing)
            res = apply_mappings(sql, mappings)
            sql = res.sql
            pending[name] = res.unmapped + [f"sql:{u}" for u in res.unqualified]
            mapped_all |= set(res.mapped)
            key = ctx.artifacts.put(f"apps/{ctx.app_id}/runs/{ctx.run_id}/sql/{name}.sql", sql.encode(),
                                    "application/sql")
            sql_keys[name] = key
            r = loaded.translation.tables[name]
            await self.propose(ctx, Fact(
                kind="sql_candidate", subject=table_key(ctx.app_id, name), trust=Trust.INFERRED,
                value={"sql_sha256": hashlib.sha256(sql.encode()).hexdigest(), "final_cte": r.final_cte,
                       "dialect": "dremio", "unmapped": res.unmapped, "unqualified": res.unqualified,
                       "mapped": res.mapped},
                evidence=[Evidence(kind="artifact", ref=key)]), result)
        chart_keys = {}
        for c in translate_charts(loaded.app, loaded.translation):
            subject = f"app:{ctx.app_id}/visual:{c.visual_id}"
            value: dict[str, Any] = {"complete": c.complete, "tier": c.tier, "tables": c.tables, "notes": c.notes,
                                     "untranslated": c.untranslated}
            evidence = [Evidence(kind="app_object", ref=f"{ctx.app_id}/visual:{c.visual_id}")]
            if c.sql:
                chart = apply_mappings(c.sql, mappings).sql
                key = ctx.artifacts.put(f"apps/{ctx.app_id}/runs/{ctx.run_id}/sql/charts/{c.visual_id}.sql",
                                        chart.encode(), "application/sql")
                chart_keys[c.visual_id] = key
                value["sql_sha256"] = hashlib.sha256(chart.encode()).hexdigest()
                evidence.append(Evidence(kind="artifact", ref=key))
            await self.propose(ctx, Fact(kind="chart_sql", subject=subject, value=value, trust=Trust.INFERRED,
                                         evidence=evidence), result)
        result.outputs.update({"sql_keys": sql_keys, "chart_keys": chart_keys,
                               "needs_dremio_dataset": sorted({s for v in pending.values() for s in v})})
        result.metrics.update({"sources_mapped": len(mapped_all),
                               "sources_unmapped": len({s for v in pending.values() for s in v})})
        result.events.append("sql.candidates")
        return result


class QueryChecker(BaseAgent):
    spec = AgentSpec(number=25, name="query_checker", version="0.2.0", kind=AgentKind.HYBRID,
                     triggers=["sql.candidates"], emits=["sql.checked"], writes=["sql_checked"],
                     routes=["repair_sql"], budget=AgentBudget(max_calls=40))

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """job: bi_ir_key, translation_key; optional tables (default: every output table) and repair (default true:
        with a model, tables failing blocking rules are repaired, at most twice each, and checked again)."""
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        tables = job.get("tables") or output_tables(loaded)
        checked = {name: await self.check_one(ctx, result, loaded, name) for name in tables}
        if ctx.gateway is not None and job.get("repair", True) and any(checked[t]["failing"] for t in checked):
            key, outcomes = await self.repair(job, ctx, [t for t in tables if checked[t]["failing"]])
            if key:
                result.outputs["translation_key"] = key
                loaded = load_translation(ctx.artifacts, job["bi_ir_key"], key)
                for o in outcomes:
                    if o["status"] == "sql_repaired":
                        checked[o["table"]] = await self.check_one(ctx, result, loaded, o["table"])
            result.outputs["repairs"] = outcomes
        result.outputs["checks"] = checked
        charts = await self.check_charts(ctx, result, loaded)
        result.outputs["chart_checks"] = charts
        result.metrics["passed"] = sum(c["ok"] for c in checked.values())
        if charts:
            result.metrics["charts_passed"] = sum(c["ok"] for c in charts.values())
        result.events.append("sql.checked")
        return result

    async def check_one(self, ctx: AgentContext, result: AgentResult, loaded: Any, name: str) -> dict[str, Any]:
        """The rule checks of one table, proposed as a sql_checked fact. Returns {ok, failing}: failing are the
        blocking rules a repair can fix (not untranslated statements)."""
        chk = check_table(loaded.translation, name)
        rules = [{"name": r.name, "ok": r.ok, "message": r.message, "blocking": r.blocking} for r in chk.rules]
        rules += [r.__dict__ for r in script_rules(loaded.app, name)]  # Qlik semantics (warnings for tables)
        await self.propose(ctx, Fact(
            kind="sql_checked", subject=table_key(ctx.app_id, name),
            trust=Trust.VALIDATED if chk.ok else Trust.INFERRED, value={"ok": chk.ok, "rules": rules},
            evidence=[Evidence(kind="rule_check", ref=f"{ctx.app_id}/{name}")]), result)
        return {"ok": chk.ok, "failing": failed_rules(loaded.translation, name),
                "warnings": [f"{r['name']}: {r['message']}" for r in rules if not r["ok"] and not r["blocking"]]}

    async def check_charts(self, ctx: AgentContext, result: AgentResult, loaded: Any) -> dict[str, Any]:
        """The BI semantics rules on each chart's SQL (fan-out, set-modifier fields, DISTINCT across tables, TOTAL /
        Aggr scope), proposed as sql_checked facts about the visual. Returns visual -> {ok, failing}."""
        out = {}
        for c in translate_charts(loaded.app, loaded.translation):
            if c.sql is None:
                continue
            rules = chart_rules(loaded.app, loaded.translation, c.visual_id, c.sql)
            ok = all(r.ok for r in rules if r.blocking)
            await self.propose(ctx, Fact(
                kind="sql_checked", subject=f"app:{ctx.app_id}/visual:{c.visual_id}",
                trust=Trust.VALIDATED if ok else Trust.INFERRED,
                value={"ok": ok, "rules": [r.__dict__ for r in rules]},
                evidence=[Evidence(kind="rule_check", ref=f"{ctx.app_id}/visual:{c.visual_id}")]), result)
            out[c.visual_id] = {"ok": ok, "failing": [f"{r.name}: {r.message}" for r in rules
                                                       if r.blocking and not r.ok]}
        return out

    async def repair(self, job: dict[str, Any], ctx: AgentContext,
                     tables: list[str]) -> tuple[str | None, list[dict[str, Any]]]:
        """Route repair_sql for each table: one statement's SQL replaced so the table passes its blocking rules.
        Returns the new translation artifact's key (None when nothing was repaired) and the outcomes."""
        doc = translation_doc(ctx.artifacts, job.get("translation_key"))
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        overrides = dict(loaded.overrides)
        outcomes = []
        for name in tables:
            o = await repair_table(loaded.app, name, ctx.gateway, overrides, task=f"{ctx.run_id}:repair")
            if o is not None:
                outcomes.append(o)
            if o is not None and o.status == "skipped":
                break
        records = [outcome_record(o) for o in outcomes]
        if not any(o.status == "sql_repaired" for o in outcomes):
            return None, records
        notes = {int(k): v for k, v in doc.get("ai_notes", {}).items()}
        notes |= {o.statement: f"sql_repaired: {o.notes}" for o in outcomes if o.status == "sql_repaired"}
        key = save_translation(ctx.artifacts, ctx.app_id, job["bi_ir_key"], overrides, notes,
                               doc.get("ai_outcomes", []) + records, doc.get("gaps", []))
        return key, records


class ReconcilerAgent(BaseAgent):
    spec = AgentSpec(number=26, name="reconciler", version="0.2.0", kind=AgentKind.HYBRID,
                     triggers=["sql.checked", "metrics.mapped"], emits=["report_sql.validated"],
                     writes=["report_sql", "figures_validated", "translation_memory", "memory_check"],
                     routes=["diagnose_mismatch"], hitl_modes=["gate"],
                     tools=[*READ_TOOLS, *WRITE_TOOLS, "get_feedback_history", "model_tools"],
                     budget=AgentBudget(max_calls=60))

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """job: bi_ir_key, translation_key, files (classified uploads, for reload log, QVDs, source extracts);
        optional diagnose (default true). Validates every output table; with a model, tables the evidence
        contradicts are diagnosed (one statement corrected, accepted only when every comparison then passes) and
        validated again; then the results are recorded."""
        result = AgentResult()
        tables = output_tables(load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key")))
        entries = {t: self.validate(job, ctx, t) for t in tables}
        if ctx.gateway is not None and job.get("diagnose", True):
            todo = [t for t, e in entries.items() if e["mismatch"]["route"] == "diagnose"]
            if todo:
                key, outcomes = await self.diagnose(job, ctx, result, todo)
                result.outputs["diagnoses"] = outcomes
                if key:
                    job = {**job, "translation_key": key}
                    result.outputs["translation_key"] = key
                    for o in outcomes:
                        if o["status"] == "ai_repaired":
                            entries[o["table"]] = self.validate(job, ctx, o["table"])
        await self.record(job, ctx, result, list(entries.values()))
        return result

    def validate(self, job: dict[str, Any], ctx: AgentContext, name: str) -> dict[str, Any]:
        """One table on the validation ladder (no facts yet): its summary, the level, the failed comparisons, the
        errors, the evidence used, and what kind of mismatch it is (adce_reversesql.diagnose)."""
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        ev = local_evidence(ctx.artifacts, job.get("files", []), loaded.app)
        tv = Reconciler(loaded.translation, ev.venue, ev.log, ev.headers, ev.contents, table_fields(loaded.app),
                        tolerance=float(job.get("tolerance") or 0.0)).validate(name)
        failed = [c.__dict__ for c in tv.comparisons if not c.ok]
        evidence = [{"kind": "query_result", "ref": f"{tv.venue}:{ctx.run_id}:{name}",
                     "detail": {"comparisons": len(tv.comparisons)}}]
        if ev.log is not None:
            evidence.append({"kind": "reload_log", "ref": ev.log_key or "reload_log", "detail": {}})
        evidence += [{"kind": "qvd_header", "ref": t, "detail": {}} for t, _ in loaded.translation.tables[name].stores
                     if t in ev.headers]
        qt = loaded.app.tables.get(name)
        stmts = {op.statement for st in qt.steps for op in st.ops} if qt else set()
        blocked = sorted({v.strip() for i in loaded.app.issues if i.code == "unresolved_variable"
                          and i.statement in stmts for v in i.message.split("unresolved:", 1)[-1].split(",")
                          if v.strip()})
        return {"table": name, "summary": tv.summary(), "level": tv.level.name if tv.level >= 0 else "none",
                "exact": tv.exact, "blocked_by": blocked,
                "validated": tv.level >= Level.V3, "venue": tv.venue, "failed": failed, "errors": tv.errors,
                "comparisons_ok": sum(c.ok for c in tv.comparisons), "comparisons": len(tv.comparisons),
                "sql_sha256": hashlib.sha256((tv.sql or "").encode()).hexdigest(), "evidence": evidence,
                "mismatch": classify(failed, tv.errors), "unreadable": ev.unreadable}

    async def diagnose(self, job: dict[str, Any], ctx: AgentContext, result: AgentResult,
                       tables: list[str]) -> tuple[str | None, list[dict[str, Any]]]:
        """Route diagnose_mismatch for each table (at most 3 answers each, judged against the evidence). Accepted
        corrections are remembered as rule fixes (translation_memory, replaces_rule). Returns the new translation
        artifact's key (None when nothing was corrected) and the outcomes."""
        doc = translation_doc(ctx.artifacts, job.get("translation_key"))
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        validator = validator_from_files(ctx.artifacts, job.get("files", []), loaded.app)
        memory = await load_memory(ctx.kg)
        known = {id(e) for e in memory.entries}
        overrides = dict(loaded.overrides)
        tools = bool(job.get("llm_tools"))
        kg_tools = await ctx.kg.tools() if tools and hasattr(ctx.kg, "tools") else []  # type: ignore[attr-defined]
        outcomes = []
        for name in tables:
            o = await diagnose_table(loaded.app, name, ctx.gateway, memory, validator, overrides,
                                     task=f"{ctx.run_id}:diagnose", tools=tools, kg_tools=kg_tools)
            if o is not None:
                outcomes.append(o)
            if o is not None and o.status == "skipped":
                break

        async def propose(f: Fact) -> str:
            return await self.propose(ctx, f, result)

        await record_memory(propose, memory, known, ctx.app_id, ctx.run_id)
        records = [outcome_record(o) for o in outcomes]
        if not any(o.status == "ai_repaired" for o in outcomes):
            return None, records
        notes = {int(k): v for k, v in doc.get("ai_notes", {}).items()}
        notes |= {o.statement: f"ai_repaired: {o.notes}" for o in outcomes if o.status == "ai_repaired"}
        key = save_translation(ctx.artifacts, ctx.app_id, job["bi_ir_key"], overrides, notes,
                               doc.get("ai_outcomes", []) + records, doc.get("gaps", []))
        return key, records

    async def record(self, job: dict[str, Any], ctx: AgentContext, result: AgentResult,
                     entries: list[dict[str, Any]]) -> None:
        """The final results: one report_sql fact per table (the review Gate), the V5 figure checks, and the
        validation report artifacts."""
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        files = job.get("files", [])
        for e in entries:
            await self.propose(ctx, Fact(
                kind="report_sql", subject=table_key(ctx.app_id, e["table"]),
                trust=Trust.VALIDATED if e["validated"] else Trust.INFERRED,
                value={"level": e["level"], "venue": e["venue"], "exact": e.get("exact", True),
                       "blocked_by": e.get("blocked_by", []),
                       "comparisons_ok": e["comparisons_ok"],
                       "comparisons": e["comparisons"], "failed": e["failed"][:20], "errors": e["errors"],
                       "sql_sha256": e["sql_sha256"]},
                evidence=[Evidence(**x) for x in e["evidence"]]), result)
        venue = local_evidence(ctx.artifacts, files, loaded.app).venue
        figures = await self._figures(ctx, result, loaded, venue, files)
        report = [e["summary"] for e in entries]
        key = put_json(ctx.artifacts, f"apps/{ctx.app_id}/runs/{ctx.run_id}/validation.json", report)
        result.artifacts.append(key)
        fkey = put_json(ctx.artifacts, f"apps/{ctx.app_id}/runs/{ctx.run_id}/figures.json", figures)
        result.artifacts.append(fkey)
        result.metrics["qvd_contents_unreadable"] = entries[0]["unreadable"] if entries else []
        result.outputs.update({"validation_key": key, "figures_key": fkey,
                               "levels": {r["table"]: r["level"] for r in report},
                               "mismatches": {e["table"]: e["mismatch"] for e in entries},
                               "chart_levels": {f["visual"]: f["level"] for f in figures if f.get("visual")}})
        result.events.append("report_sql.validated")

    async def _figures(self, ctx: AgentContext, result: AgentResult, loaded: Any, venue: Any,
                       files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """V5: compare each exported report with the SQL of the chart it came from (local venue until phase PD)."""
        exports = {f["name"]: ctx.artifacts.get(f["key"]) for f in files if f["class"] == "report_export"}
        if not exports or not loaded.app.visuals:
            return []
        out = []
        keys = {f["name"]: f["key"] for f in files}
        for r in check_exports(loaded.app, loaded.translation, venue, exports):
            c = r.check
            entry: dict[str, Any] = {"export": r.file, "sheet": r.sheet,
                                     "visual": r.match.visual_id if r.match else None,
                                     "level": "V5" if r.level_v5 else ("compared" if c else "not compared"),
                                     "venue": "local", "problems": r.problems}
            if c is not None:
                entry.update({"figures": c.figures, "matched": c.matched, "totals": c.totals_checked,
                              "totals_matched": c.totals_matched, "rows_expected": c.rows_expected,
                              "rows_actual": c.rows_actual, "missing_rows": [list(x) for x in c.missing_rows[:20]],
                              "extra_rows": [list(x) for x in c.extra_rows[:20]],
                              "diffs": [{"row": list(d.row), "measure": d.measure, "expected": d.expected,
                                         "actual": d.actual} for d in c.diffs[:20]]})
            out.append(entry)
            if entry["visual"]:
                await self.propose(ctx, Fact(
                    kind="figures_validated", subject=f"app:{ctx.app_id}/visual:{entry['visual']}",
                    trust=Trust.VALIDATED if r.level_v5 else Trust.INFERRED,
                    value={k: v for k, v in entry.items() if k != "visual"},
                    evidence=[Evidence(kind="export", ref=keys.get(r.file, r.file), detail={"sheet": r.sheet}),
                              Evidence(kind="query_result", ref=f"local:{ctx.run_id}:chart:{entry['visual']}")]),
                    result)
        result.metrics["charts_v5"] = sum(1 for f in out if f["level"] == "V5")
        return out


def lineage(app_id: str, loaded: Any) -> dict[str, Any]:
    """OpenLineage-shaped dataset lineage: one job per Qlik table, inputs = sources / resident tables / QVDs."""
    jobs = []
    for name, qt in loaded.app.tables.items():
        if qt.mapping:
            continue
        inputs: list[dict[str, str]] = []
        for step in qt.steps:
            for op in step.ops:
                s = op.source
                if s.kind == "sql":
                    inputs += [{"namespace": f"dremio://{s.connection or 'default'}", "name": t} for t in s.tables]
                elif s.kind == "resident":
                    inputs.append({"namespace": f"qlik://{app_id}", "name": s.ref or ""})
                elif s.kind in ("qvd", "file"):
                    inputs.append({"namespace": f"{s.kind}://", "name": s.ref or ""})
        outputs = [{"namespace": f"qlik://{app_id}", "name": name}] + [
            {"namespace": "qvd://", "name": t} for t in qt.stored_to]
        uniq = list({json.dumps(i, sort_keys=True): i for i in inputs}.values())
        jobs.append({"job": {"namespace": f"qlik://{app_id}", "name": f"load:{name}"}, "inputs": uniq,
                     "outputs": outputs, "dropped": qt.dropped_at is not None})
    return {"producer": "adce/lineage-lite@0.1.0", "schemaURL": "https://openlineage.io/spec", "jobs": jobs}


class Publisher(BaseAgent):
    spec = AgentSpec(number=20, name="publisher_lite", version="0.1.0", kind=AgentKind.DETERMINISTIC,
                     triggers=["report_sql.validated"], emits=["pack.published"], writes=["pack_published", "lineage"],
                     group="definition", hitl_modes=["gate"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """Build the pack in memory (pack.py), then write it as a new version, or reuse the version with the same
        content (the same inputs and versions give the same bytes, ADR-Q7)."""
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        validation = json.loads(ctx.artifacts.get(job["validation_key"])) if job.get("validation_key") else []
        figures = json.loads(ctx.artifacts.get(job["figures_key"])) if job.get("figures_key") else []
        mappings = await known_mappings(ctx.kg, job.get("source_map"))
        body: dict[str, bytes] = {}
        for name in output_tables(loaded):
            sql = loaded.translation.table_sql(name)
            if sql:
                body[f"sql/tables/{name}.sql"] = apply_mappings(sql, mappings).sql.encode()
        charts = translate_charts(loaded.app, loaded.translation)
        for ch in charts:
            if ch.sql:
                body[f"sql/measures/{ch.visual_id}.sql"] = apply_mappings(ch.sql, mappings).sql.encode()
        lin = lineage(ctx.app_id, loaded)
        body["lineage/lineage.json"] = json.dumps(lin, indent=2, sort_keys=True).encode()
        body["lineage/lineage.md"] = pack.lineage_md(ctx.app_id, lin).encode()
        body["validation/report.md"] = pack.validation_md(validation, figures).encode()
        body["validation/report.xlsx"] = pack.validation_xlsx(validation, figures)
        knowledge = await self._knowledge(ctx, loaded, lin)
        body["review/untranslated.csv"] = pack.untranslated_csv(loaded, charts)
        body["review/decisions.csv"] = pack.decisions_csv(knowledge["decisions"])
        body["review/open-items.md"] = pack.open_items_md(
            loaded, charts, unmapped=knowledge["unmapped"], missing_producers=knowledge["missing_producers"],
            not_approved=knowledge["not_approved"]).encode()
        content = pack.fingerprint(body)

        root = f"apps/{ctx.app_id}/packs"
        reload_time = loaded.app.reload_time
        versions: dict[int, str] = {}  # this reload's versions -> their content fingerprint
        for key in ctx.artifacts.list(f"{root}/"):
            if key.endswith("/manifest.json"):
                m = json.loads(ctx.artifacts.get(key))
                if m.get("reload_time") == reload_time and m.get("version"):
                    versions[int(m["version"])] = m.get("content_sha256", "")
        same = [v for v, sha in versions.items() if sha == content]
        version = same[0] if same else max(versions, default=0) + 1
        name = pack.pack_name(ctx.app_id, reload_time, version)
        base = f"{root}/{name}"
        if not same:  # a new version: write it; an identical one is already there, byte for byte
            for path, data in body.items():
                ctx.artifacts.put(f"{base}/{path}", data, _content_type(path))
            manifest = {"app_id": ctx.app_id, "pack": name, "version": version, "reload_time": reload_time,
                        "run_id": ctx.run_id, "inputs": sorted(job.get("inputs", []), key=lambda i: i["name"]),
                        "bi_ir_key": job["bi_ir_key"], "content_sha256": content,
                        "models": knowledge["models"], **pack.versions(),
                        "venue": "local (Dremio validation pending, phase PD)", "files": sorted(body)}
            put_json(ctx.artifacts, f"{base}/manifest.json", manifest)
        files = {path: f"{base}/{path}" for path in [*body, "manifest.json"]}
        for job_ in lin["jobs"]:  # lineage as knowledge (19-lite): what each Qlik table is derived from (DERIVED_FROM)
            table = job_["job"]["name"].removeprefix("load:")
            await self.propose(ctx, Fact(kind="lineage", subject=table_key(ctx.app_id, table), trust=Trust.DOCUMENTED,
                                         value={"inputs": job_["inputs"], "outputs": job_["outputs"],
                                                "dropped": job_["dropped"]},
                                         evidence=[Evidence(kind="bi_ir", ref=job["bi_ir_key"])]), result)
        await self.propose(ctx, Fact(kind="pack_published", subject=f"app:{ctx.app_id}", trust=Trust.DOCUMENTED,
                                     value={"run_id": ctx.run_id, "pack": name, "version": version,
                                            "reused": bool(same), "files": sorted(files), "prefix": base},
                                     evidence=[Evidence(kind="artifact", ref=files["manifest.json"])]), result)
        result.outputs.update({"pack_prefix": base, "pack": name, "files": files})
        result.metrics.update({"pack_version": version, "pack_reused": bool(same), "pack_files": len(files)})
        result.events.append("pack.published")
        return result

    async def _knowledge(self, ctx: AgentContext, loaded: Any, lin: dict[str, Any]) -> dict[str, Any]:
        """What the review files need from the knowledge: decisions applied, unmapped sources, QVDs whose producer
        is missing, tables not approved, and the models and prompt versions behind the app's facts."""
        app_key = f"app:{ctx.app_id}"
        mine = [f for f in await ctx.kg.facts() if f.subject == app_key or f.subject.startswith(app_key + "/")]
        read = {f.object for f in mine if f.kind == "reads_source" and f.object}
        decided = [f for f in await ctx.kg.facts(kind="resolves_to") if f.subject in read]
        decisions = [f for f in mine + decided if "_decided_by" in f.value or f.kind == "auto_accepted"]
        mapped = {f.subject for f in decided if f.trust == Trust.CONFIRMED}
        unmapped = sorted({s for s in read if s.startswith("source:")} - mapped)
        made = {o["name"] for j in lin["jobs"] for o in j["outputs"] if o["namespace"] == "qvd://"}
        read_qvds = {i["name"] for j in lin["jobs"] for i in j["inputs"] if i["namespace"] == "qvd://"}
        linked = {f.value.get("qvd") for f in mine if f.kind == "qvd_linkage"}
        missing = sorted(q for q in read_qvds - made if q not in linked and f"qvd:{q}" not in linked)
        not_approved = sorted(f.subject.split("/table:")[-1] for f in mine
                              if f.kind == "report_sql" and f.trust != Trust.CONFIRMED)
        models: dict[tuple[str, str], int] = {}
        for f in mine:
            m = f.model or f.value.get("model")
            if m:
                k = (str(m), str(f.prompt_version or f.value.get("prompt_version") or ""))
                models[k] = models.get(k, 0) + 1
        return {"decisions": decisions, "unmapped": unmapped, "missing_producers": missing,
                "not_approved": not_approved,
                "models": [{"model": m, "prompt_version": p, "facts": n} for (m, p), n in sorted(models.items())]}


def _content_type(path: str) -> str:
    return {"sql": "application/sql", "json": "application/json", "md": "text/markdown", "csv": "text/csv",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[path.rsplit(".", 1)[-1]]
