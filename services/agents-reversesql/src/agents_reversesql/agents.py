"""Reverse-SQL agents for the Qlik path.

24 Synthesizer     renders the Dremio SQL per table (CTE chain with statement map) and per chart as artifacts
25 Query checker   runs the rule checks (syntax, translation, fields, determinism, inputs)
26 Reconciler      executes in the venue available (local DuckDB until phase PD) and climbs the validation ladder;
                   charts with an exported report reach V5 when every figure matches
19 Lineage (lite)  dataset-level lineage from logical sources through Qlik tables to QVDs, OpenLineage-shaped
20 Publisher (lite) writes the migration pack: SQL, lineage, validation report, review record, manifest
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.artifacts import put_json
from adce_core.facts import Evidence, Fact, Trust
from adce_qlik import parse_reload_log, read_qvd_header
from adce_reversesql.charts import translate_charts
from adce_reversesql.check import check_table
from adce_reversesql.figures import check_exports
from adce_reversesql.local import read_qvd
from adce_reversesql.reconcile import Level, Reconciler
from adce_reversesql.runtime import load_translation, local_venue_from_files


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
                     writes=["sql_candidate", "chart_sql"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        sql_keys = {}
        for name in output_tables(loaded):
            sql = loaded.translation.table_sql(name)
            if sql is None:
                continue
            key = ctx.artifacts.put(f"apps/{ctx.app_id}/runs/{ctx.run_id}/sql/{name}.sql", sql.encode(),
                                    "application/sql")
            sql_keys[name] = key
            r = loaded.translation.tables[name]
            await self.propose(ctx, Fact(
                kind="sql_candidate", subject=table_key(ctx.app_id, name), trust=Trust.INFERRED,
                value={"sql_sha256": hashlib.sha256(sql.encode()).hexdigest(), "final_cte": r.final_cte,
                       "dialect": "dremio"},
                evidence=[Evidence(kind="artifact", ref=key)]), result)
        chart_keys = {}
        for c in translate_charts(loaded.app, loaded.translation):
            subject = f"app:{ctx.app_id}/visual:{c.visual_id}"
            value: dict[str, Any] = {"complete": c.complete, "tier": c.tier, "tables": c.tables, "notes": c.notes,
                                     "untranslated": c.untranslated}
            evidence = [Evidence(kind="app_object", ref=f"{ctx.app_id}/visual:{c.visual_id}")]
            if c.sql:
                key = ctx.artifacts.put(f"apps/{ctx.app_id}/runs/{ctx.run_id}/sql/charts/{c.visual_id}.sql",
                                        c.sql.encode(), "application/sql")
                chart_keys[c.visual_id] = key
                value["sql_sha256"] = hashlib.sha256(c.sql.encode()).hexdigest()
                evidence.append(Evidence(kind="artifact", ref=key))
            await self.propose(ctx, Fact(kind="chart_sql", subject=subject, value=value, trust=Trust.INFERRED,
                                         evidence=evidence), result)
        result.outputs.update({"sql_keys": sql_keys, "chart_keys": chart_keys})
        result.events.append("sql.candidates")
        return result


class QueryChecker(BaseAgent):
    spec = AgentSpec(number=25, name="query_checker", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["sql.candidates"], emits=["sql.checked"], writes=["sql_checked"],
                     routes=["repair_sql"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        passed = 0
        for name in output_tables(loaded):
            chk = check_table(loaded.translation, name)
            passed += chk.ok
            await self.propose(ctx, Fact(
                kind="sql_checked", subject=table_key(ctx.app_id, name),
                trust=Trust.VALIDATED if chk.ok else Trust.INFERRED,
                value={"ok": chk.ok, "rules": [{"name": r.name, "ok": r.ok, "message": r.message,
                                                "blocking": r.blocking} for r in chk.rules]},
                evidence=[Evidence(kind="rule_check", ref=f"{ctx.app_id}/{name}")]), result)
        result.metrics["passed"] = passed
        result.events.append("sql.checked")
        return result


class ReconcilerAgent(BaseAgent):
    spec = AgentSpec(number=26, name="reconciler", version="0.1.0", kind=AgentKind.HYBRID,
                     triggers=["sql.checked", "metrics.mapped"], emits=["report_sql.validated"],
                     writes=["report_sql", "figures_validated"], routes=["diagnose_mismatch"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """job: bi_ir_key, translation_key, files (classified uploads, for reload log, QVDs, source extracts)."""
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        files = job.get("files", [])
        logs = [f for f in files if f["class"] == "reload_log"]
        log = parse_reload_log(ctx.artifacts.get(logs[-1]["key"]).decode("utf-8", errors="replace")) if logs else None
        venue, qvd_paths = local_venue_from_files(ctx.artifacts, files)
        targets = {PurePosixPath(s.target.replace("\\", "/")).name.lower(): s.target for s in loaded.app.stores}
        headers, contents = {}, {}
        unreadable: list[str] = []
        for fname, path in qvd_paths.items():
            target = targets.get(fname)
            try:
                data = read_qvd(path)
            except Exception:  # noqa: BLE001 - header-only or damaged QVD: headers still give row counts (V3)
                data = None
                unreadable.append(fname)
            if target is None:
                if data is not None:
                    venue.add_qvd(f"lib://local/{fname}", path)  # an input QVD read by this app
                continue
            headers[target] = read_qvd_header(path)
            if data is not None:
                contents[target] = data
                venue.add_qvd(target, path)  # a stored QVD may also be read back later in the same script
        result.metrics["qvd_contents_unreadable"] = unreadable
        rec = Reconciler(loaded.translation, venue, log, headers, contents)
        report = []
        for name in output_tables(loaded):
            tv = rec.validate(name)
            report.append(tv.summary())
            failed = [c.__dict__ for c in tv.comparisons if not c.ok]
            evidence = [Evidence(kind="query_result", ref=f"{tv.venue}:{ctx.run_id}:{name}",
                                 detail={"comparisons": len(tv.comparisons)})]
            if log is not None:
                evidence.append(Evidence(kind="reload_log", ref=logs[-1]["key"]))
            evidence += [Evidence(kind="qvd_header", ref=t) for t, _ in loaded.translation.tables[name].stores
                         if t in headers]
            await self.propose(ctx, Fact(
                kind="report_sql", subject=table_key(ctx.app_id, name),
                trust=Trust.VALIDATED if tv.level >= Level.V3 else Trust.INFERRED,
                value={"level": tv.level.name if tv.level >= 0 else "none", "venue": tv.venue,
                       "comparisons_ok": sum(c.ok for c in tv.comparisons), "comparisons": len(tv.comparisons),
                       "failed": failed[:20], "errors": tv.errors,
                       "sql_sha256": hashlib.sha256((tv.sql or "").encode()).hexdigest()},
                evidence=evidence), result)
        figures = await self._figures(ctx, result, loaded, venue, files)
        key = put_json(ctx.artifacts, f"apps/{ctx.app_id}/runs/{ctx.run_id}/validation.json", report)
        result.artifacts.append(key)
        fkey = put_json(ctx.artifacts, f"apps/{ctx.app_id}/runs/{ctx.run_id}/figures.json", figures)
        result.artifacts.append(fkey)
        result.outputs.update({"validation_key": key, "figures_key": fkey,
                               "levels": {r["table"]: r["level"] for r in report},
                               "chart_levels": {f["visual"]: f["level"] for f in figures if f.get("visual")}})
        result.events.append("report_sql.validated")
        return result


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
                     triggers=["report_sql.validated"], emits=["pack.published"], writes=["pack_published"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        result = AgentResult()
        loaded = load_translation(ctx.artifacts, job["bi_ir_key"], job.get("translation_key"))
        base = f"apps/{ctx.app_id}/packs/{ctx.run_id}"
        validation = json.loads(ctx.artifacts.get(job["validation_key"])) if job.get("validation_key") else []
        figures = json.loads(ctx.artifacts.get(job["figures_key"])) if job.get("figures_key") else []
        files: dict[str, str] = {}
        for name in output_tables(loaded):
            sql = loaded.translation.table_sql(name)
            if sql:
                files[f"sql/{name}.sql"] = ctx.artifacts.put(f"{base}/sql/{name}.sql", sql.encode(), "application/sql")
        charts = translate_charts(loaded.app, loaded.translation)
        for c in charts:
            if c.sql:
                files[f"sql/charts/{c.visual_id}.sql"] = ctx.artifacts.put(f"{base}/sql/charts/{c.visual_id}.sql",
                                                                           c.sql.encode(), "application/sql")
        files["lineage.json"] = put_json(ctx.artifacts, f"{base}/lineage.json", lineage(ctx.app_id, loaded))
        files["validation.md"] = ctx.artifacts.put(f"{base}/validation.md",
                                                   _validation_md(validation, figures).encode(), "text/markdown")
        files["review.md"] = ctx.artifacts.put(f"{base}/review.md", _review_md(loaded, charts).encode(),
                                               "text/markdown")
        manifest = {"app_id": ctx.app_id, "run_id": ctx.run_id, "inputs": job.get("inputs", []),
                    "bi_ir_key": job["bi_ir_key"], "translation_key": job.get("translation_key"),
                    "software": {"adce": "0.1.0"}, "venue": "local (Dremio validation pending, phase PD)",
                    "files": sorted(files)}
        files["manifest.json"] = put_json(ctx.artifacts, f"{base}/manifest.json", manifest)
        await self.propose(ctx, Fact(kind="pack_published", subject=f"app:{ctx.app_id}", trust=Trust.DOCUMENTED,
                                     value={"run_id": ctx.run_id, "files": sorted(files), "prefix": base},
                                     evidence=[Evidence(kind="artifact", ref=files["manifest.json"])]), result)
        result.outputs.update({"pack_prefix": base, "files": files})
        result.events.append("pack.published")
        return result


def _validation_md(report: list[dict[str, Any]], figures: list[dict[str, Any]] | None = None) -> str:
    lines = ["# Validation report", "",
             "Levels: V0 parsed · V1 checked · V2 executes · V3 row counts · V4 data · V5 figures match exports.",
             "Venue `local` means DuckDB over QVD and source extracts; results are re-earned in Dremio in phase PD.",
             "", "| Table | Level | Venue | Comparisons | Failed |", "|---|---|---|---|---|"]
    for r in report:
        comps = r["comparisons"]
        bad = [c for c in comps if not c["ok"]]
        lines.append(f"| {r['table']} | {r['level']} | {r['venue']} | {len(comps)} | {len(bad)} |")
    for r in report:
        bad = [c for c in r["comparisons"] if not c["ok"]]
        failed_rules = [x for x in r["rules"] if not x["ok"]]
        if bad or failed_rules or r["errors"]:
            lines += ["", f"## {r['table']}"]
            lines += [f"- rule `{x['name']}`: {x['message']}" for x in failed_rules]
            lines += [f"- {c['check']} {c['subject']}: expected {c['expected']}, got {c['actual']}" for c in bad]
            lines += [f"- error: {e}" for e in r["errors"]]
    if figures:
        lines += ["", "## Charts against exported reports", "",
                  "| Export | Visual | Level | Figures | Totals | Rows (SQL / export) |", "|---|---|---|---|---|---|"]
        for f in figures:
            lines.append(f"| {f['export']} [{f['sheet']}] | {f['visual'] or '-'} | {f['level']} | "
                         f"{f.get('matched', 0)}/{f.get('figures', 0)} | {f.get('totals_matched', 0)}/"
                         f"{f.get('totals', 0)} | {f.get('rows_actual', '-')} / {f.get('rows_expected', '-')} |")
        for f in figures:
            if f["level"] != "V5":
                lines += ["", f"### {f['export']} [{f['sheet']}]"]
                lines += [f"- {p}" for p in f.get("problems", [])]
                lines += [f"- {d['row']} {d['measure']}: expected {d['expected']}, got {d['actual']}"
                          for d in f.get("diffs", [])]
    return "\n".join(lines) + "\n"


def _review_md(loaded: Any, charts: list[Any] | None = None) -> str:
    lines = ["# Review record", "", "## Statements not translated", ""]
    items = [(n, u) for n, r in loaded.translation.tables.items() for u in r.untranslated]
    lines += [f"- {n}: statement {u.statement} ({u.span}) - {u.reason} [{u.tier}]" for n, u in items] or ["- none"]
    if charts:
        lines += ["", "## Charts not translated", ""]
        lines += [f"- {c.visual_id} {c.title!r}: {'; '.join(c.untranslated)} [{c.tier}]" for c in charts
                  if c.untranslated] or ["- none"]
    lines += ["", "## Issues found while reading the script", ""]
    lines += [f"- {i.code}: {i.message}" for i in loaded.app.issues] or ["- none"]
    inputs = sorted({i for r in loaded.translation.tables.values() for i in r.inputs})
    lines += ["", "## Inputs to resolve before running in Dremio", ""]
    lines += [f"- {i}" for i in inputs] or ["- none"]
    return "\n".join(lines) + "\n"
