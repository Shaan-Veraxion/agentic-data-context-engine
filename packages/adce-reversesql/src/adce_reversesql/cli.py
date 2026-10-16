"""adce: command line for the Qlik path, without services.

    adce parse <app_dir>                     statements, coverage, tables, issues
    adce translate <app_dir> [--table T]     Dremio SQL per table
    adce validate <app_dir>                  local validation (needs <app_dir>/sources and/or qvd/)
    adce report <app_dir>... [--json]        coverage and translation measures, and what blocks the rest
  --ai on translate / validate / report: statements the rules leave untranslated go through the LLM pathway
       (model gateway, translation memory, checks, local validation when data exists). Settings come from the
       environment and deploy/compose/.env (ADCE_GATEWAY_CONFIG, provider keys); memory and recordings are kept
       in .adce/ (git-ignored: they can contain client script text).
    adce charts <app_dir> [--visual ID]      Dremio SQL per chart (needs <app_dir>/frontend)

An app directory holds *.qvs scripts, an optional reload.log (or *.log), qvd/*.qvd and sources/<path>.csv|parquet,
and optionally connections.json ({"<connection>": "<dialect>"}) naming the SQL dialect of a connection,
frontend/**/*.json (Qlik Sense app objects, e.g. from `qlik app unbuild`) and exports/*.xlsx|csv (exported reports,
for V5).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adce_core.bi_ir import BIApp
from adce_qlik import parse_reload_log, read_qvd_header, read_script
from adce_qlik.frontend import attach_frontend
from adce_qlik.model import build_app
from adce_qlik.reload_log import ReloadLog

from .compose import Translation, compose
from .local import LocalVenue, read_qvd
from .reconcile import Reconciler
from .runtime import table_fields


def _load(app_dir: Path) -> tuple[ReloadLog | None, BIApp]:
    scripts = {p.name: p.read_text(encoding="utf-8-sig") for p in sorted(app_dir.glob("*.qvs"))}
    if not scripts:
        raise SystemExit(f"no .qvs scripts in {app_dir}")
    logs = sorted(app_dir.glob("*.log"))
    log = parse_reload_log(logs[-1].read_text(encoding="utf-8", errors="replace")) if logs else None
    qvd_cols = {}
    for q in sorted((app_dir / "qvd").glob("*.qvd")):
        qvd_cols[q.name] = [f.name for f in read_qvd_header(q).fields]
    doc = read_script(app_dir.name, scripts, log)
    conn_file = app_dir / "connections.json"  # optional: {"<connection name>": "<sqlglot dialect>"}
    dialects = json.loads(conn_file.read_text(encoding="utf-8")) if conn_file.is_file() else None
    app = build_app(doc, qvd_columns=qvd_cols, connection_dialects=dialects)
    fe = {str(p.relative_to(app_dir)): p.read_text(encoding="utf-8-sig")
          for p in sorted((app_dir / "frontend").rglob("*.json"))}
    if fe:
        attach_frontend(app, fe)
    return log, app


def _venue(d: Path, app: object) -> tuple[LocalVenue, dict, dict]:  # type: ignore[type-arg]
    venue = LocalVenue()
    if (d / "sources").exists():
        venue.add_source_dir(d / "sources")
    heads, conts = {}, {}
    stores = {Path(s.target.replace("\\", "/")).name.lower(): s.target for s in app.stores}  # type: ignore[attr-defined]
    for q in sorted((d / "qvd").glob("*.qvd")):
        target = stores.get(q.name.lower())
        if target:
            heads[target] = read_qvd_header(q)
            conts[target] = read_qvd(q)
            venue.add_qvd(target, q)
        else:
            venue.add_qvd(f"lib://local/{q.name}", q)
    return venue, heads, conts


def _env_file(path: str = "deploy/compose/.env") -> None:
    import os

    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _gateway() -> object:
    from adce_core.settings import Settings
    from adce_gateway.config import default_config, load_config
    from adce_gateway.gateway import ModelGateway, Recordings
    from adce_gateway.tracing import langfuse_tracer

    _env_file()
    s = Settings()
    config = load_config(s.gateway_config) if s.gateway_config else default_config(s.vertex_region)
    mode = s.gateway_mode if s.gateway_mode != "replay" else "record"
    return ModelGateway(mode, Recordings(".adce/recordings"), config=config, tracer=langfuse_tracer())


def _with_ai(d: Path, app: object, log: ReloadLog | None, quiet: bool = False) -> tuple[Translation, list]:  # type: ignore[type-arg]
    """Run the LLM pathway; returns the translation with accepted overrides and the outcomes."""
    import asyncio

    from .ai_loop import TranslationMemory, repair_with_ai, translate_with_ai

    venue, heads, conts = _venue(d, app)

    from .runtime import make_validator

    validator = make_validator(venue, log, heads, conts)

    memory = TranslationMemory(".adce/memory.json")

    def show(o: object) -> None:
        if not quiet:
            print(f"-- ai: {o.table} statement {o.statement}: {o.status}"  # type: ignore[attr-defined]
                  f"{' after ' + str(len(o.attempts)) + ' attempt(s)' if o.attempts else ''}",  # type: ignore[attr-defined]
                  file=sys.stderr, flush=True)

    gateway = _gateway()

    async def both() -> tuple[dict[int, str], list]:  # type: ignore[type-arg]
        ov, out = await translate_with_ai(app, gateway, memory, validator=validator,  # type: ignore[arg-type]
                                          task=f"cli:{d.name}", on_outcome=show)
        ov, fixes = await repair_with_ai(app, gateway, memory, validator, overrides=ov,  # type: ignore[arg-type]
                                         task=f"cli:{d.name}:repair", on_outcome=show)
        return ov, out + fixes

    overrides, outcomes = asyncio.run(both())
    memory.save()
    return compose(app, overrides), outcomes  # type: ignore[arg-type]


def cmd_parse(args: argparse.Namespace) -> int:
    _, app = _load(Path(args.app_dir))
    out = {"coverage": app.coverage.model_dump() | {"ratio": round(app.coverage.ratio, 4)},  # type: ignore[attr-defined]
           "tables": {t.name: {"fields": t.fields, "mapping": t.mapping, "dropped": t.dropped_at is not None,
                               "steps": [s.action.value for s in t.steps], "stored_to": t.stored_to}
                      for t in app.tables.values()},  # type: ignore[attr-defined]
           "sources": [s.key for s in app.sources],  # type: ignore[attr-defined]
           "issues": [f"{i.code}: {i.message}" for i in app.issues]}  # type: ignore[attr-defined]
    print(json.dumps(out, indent=2))
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    log, app = _load(Path(args.app_dir))
    t = _with_ai(Path(args.app_dir), app, log)[0] if args.ai else compose(app)  # type: ignore[arg-type]
    for name, r in t.tables.items():  # type: ignore[attr-defined]
        if args.table and name != args.table:
            continue
        sql = t.table_sql(name)  # type: ignore[attr-defined]
        print(f"-- ===== {name} ({'complete' if r.complete else 'incomplete'})")
        for u in r.untranslated:
            print(f"-- not translated: statement {u.statement} ({u.span}): {u.reason} [{u.tier}]")
        if sql:
            print(sql + ";\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    d = Path(args.app_dir)
    log, app = _load(d)
    t = _with_ai(d, app, log)[0] if args.ai else compose(app)  # type: ignore[arg-type]
    venue, heads, conts = _venue(d, app)
    rec = Reconciler(t, venue, log, heads, conts, table_fields(app))
    worst = 0
    for name, qt in app.tables.items():  # type: ignore[attr-defined]
        if qt.mapping or (qt.dropped_at is not None and not t.tables[name].stores):
            continue
        tv = rec.validate(name)
        bad = [c for c in tv.comparisons if not c.ok]
        print(f"{name}: {tv.level.name if tv.level >= 0 else 'none'} ({tv.venue}), "
              f"{len(tv.comparisons) - len(bad)}/{len(tv.comparisons)} comparisons ok")
        for c in bad:
            print(f"   x {c.check} {c.subject}: expected {c.expected}, got {c.actual}")
        for r in tv.check.failed():
            print(f"   - rule {r.name}: {r.message}")
        worst = max(worst, 0 if tv.level >= 3 else 1)
    exports = {p.name: p.read_bytes() for p in sorted((d / "exports").glob("*")) if p.is_file()}
    if exports:
        from .figures import check_exports

        print()
        for r in check_exports(app, t, venue, exports):  # type: ignore[arg-type]
            if r.check is None:
                print(f"{r.file} [{r.sheet}]: not compared — {'; '.join(r.problems)}")
                continue
            c = r.check
            print(f"{r.file} [{r.sheet}] -> {c.visual_id}: {'V5 (local)' if c.ok else 'figures differ'}, "
                  f"{c.matched}/{c.figures} figures, totals {c.totals_matched}/{c.totals_checked}, "
                  f"rows {c.rows_actual} produced / {c.rows_expected} exported")
            for x in c.diffs[:10]:
                print(f"   x {x.row} {x.measure}: expected {x.expected}, got {x.actual}")
            for k in c.missing_rows[:5]:
                print(f"   - row in export only: {k}")
            for k in c.extra_rows[:5]:
                print(f"   + row in SQL only: {k}")
            worst = max(worst, 0 if c.ok else 1)
    return worst


def cmd_charts(args: argparse.Namespace) -> int:
    from .charts import translate_charts

    _, app = _load(Path(args.app_dir))
    charts = translate_charts(app, compose(app))  # type: ignore[arg-type]
    if not charts:
        print("no visuals: add the app's objects under <app_dir>/frontend")
        return 1
    for c in charts:
        if args.visual and c.visual_id != args.visual:
            continue
        print(f"-- ===== {c.visual_id} {c.title!r} ({'complete' if c.complete else 'incomplete'}, {c.tier})")
        for n in c.notes:
            print(f"-- note: {n}")
        for u in c.untranslated:
            print(f"-- not translated: {u}")
        if c.sql:
            print(c.sql + ";\n")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .measures import measure

    rows = []
    for d in args.app_dirs:
        log, app = _load(Path(d))
        if args.ai:
            t, outcomes = _with_ai(Path(d), app, log)
            rows.append(measure(app, t, outcomes))  # type: ignore[arg-type]
        else:
            rows.append(measure(app, compose(app)))  # type: ignore[arg-type]
    if args.json:
        print(json.dumps([m.as_dict() for m in rows], indent=2))
        return 0
    print(f"{'app':24} {'understood':>11} {'translated':>11} {'tables done':>12}")
    for m in rows:
        print(f"{m.app_id[:24]:24} {m.understood_ratio:>10.1%} {m.translated_ratio:>10.1%} "
              f"{m.tables_complete:>5}/{m.tables:<6}")
    st = sum(m.table_statements for m in rows)
    tr = sum(m.table_statements_translated for m in rows)
    print(f"{'all':24} {'':>11} {tr / st if st else 1:>10.1%}   ({tr}/{st} table statements)")
    for m in rows:
        print()
        print(f"{m.app_id}: dialects {m.connection_dialects or '-'}; {len(m.unresolved_variables)} variables "
              f"set outside the script; {m.inputs_to_resolve} inputs to resolve; {m.knock_on} knock-on statements")
        if m.ai:
            print("   by the LLM pathway: " + ", ".join(f"{k} {v}" for k, v in m.ai.items()))
        for tier, reason, n in m.root_causes:
            print(f"   [{tier}] x{n} {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adce", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("parse", cmd_parse), ("translate", cmd_translate), ("validate", cmd_validate)):
        sp = sub.add_parser(name)
        sp.add_argument("app_dir")
        if name == "translate":
            sp.add_argument("--table")
        if name in ("translate", "validate"):
            sp.add_argument("--ai", action="store_true", help="use the LLM pathway for what the rules cannot do")
        sp.set_defaults(fn=fn)
    cp = sub.add_parser("charts", help="Dremio SQL per chart and table of the front end")
    cp.add_argument("app_dir")
    cp.add_argument("--visual")
    cp.set_defaults(fn=cmd_charts)
    rp = sub.add_parser("report", help="coverage and translation measures for one or more app folders")
    rp.add_argument("app_dirs", nargs="+")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--ai", action="store_true", help="use the LLM pathway for what the rules cannot do")
    rp.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
