"""adce: command line for the Qlik path, without services.

    adce parse <app_dir>                     statements, coverage, tables, issues
    adce translate <app_dir> [--table T]     Dremio SQL per table
    adce validate <app_dir>                  local validation (needs <app_dir>/sources and/or qvd/)
    adce report <app_dir>... [--json]        coverage and translation measures, and what blocks the rest
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

from adce_qlik import parse_reload_log, read_qvd_header, read_script
from adce_qlik.frontend import attach_frontend
from adce_qlik.model import build_app
from adce_qlik.reload_log import ReloadLog

from .compose import compose
from .local import LocalVenue, read_qvd
from .reconcile import Reconciler


def _load(app_dir: Path) -> tuple[ReloadLog | None, object]:
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
    _, app = _load(Path(args.app_dir))
    t = compose(app)  # type: ignore[arg-type]
    for name, r in t.tables.items():
        if args.table and name != args.table:
            continue
        sql = t.table_sql(name)
        print(f"-- ===== {name} ({'complete' if r.complete else 'incomplete'})")
        for u in r.untranslated:
            print(f"-- not translated: statement {u.statement} ({u.span}): {u.reason} [{u.tier}]")
        if sql:
            print(sql + ";\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    d = Path(args.app_dir)
    log, app = _load(d)
    t = compose(app)  # type: ignore[arg-type]
    venue, heads, conts = _venue(d, app)
    rec = Reconciler(t, venue, log, heads, conts)
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
        _, app = _load(Path(d))
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
        sp.set_defaults(fn=fn)
    cp = sub.add_parser("charts", help="Dremio SQL per chart and table of the front end")
    cp.add_argument("app_dir")
    cp.add_argument("--visual")
    cp.set_defaults(fn=cmd_charts)
    rp = sub.add_parser("report", help="coverage and translation measures for one or more app folders")
    rp.add_argument("app_dirs", nargs="+")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
