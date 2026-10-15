"""adce: command line for the Qlik path, without services.

    adce parse <app_dir>                     statements, coverage, tables, issues
    adce translate <app_dir> [--table T]     Dremio SQL per table
    adce validate <app_dir>                  local validation (needs <app_dir>/sources and/or qvd/)

An app directory holds *.qvs scripts, an optional reload.log (or *.log), qvd/*.qvd, sources/<path>.csv|parquet and
an optional connections.toml (dialect and Dremio location of databases queried directly, see sources.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adce_qlik import parse_reload_log, read_qvd_header, read_script
from adce_qlik.model import build_app
from adce_qlik.reload_log import ReloadLog

from .compose import compose
from .local import LocalVenue, read_qvd
from .reconcile import Reconciler
from .sources import Connection, load_connections


def _connections(app_dir: Path) -> dict[str, Connection]:
    return load_connections(app_dir / "connections.toml")


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
    dialects = {name: c.dialect for name, c in _connections(app_dir).items()}
    return log, build_app(doc, qvd_columns=qvd_cols, connection_dialects=dialects)


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
    t = compose(app, connections=_connections(Path(args.app_dir)))  # type: ignore[arg-type]
    for name, r in t.tables.items():
        if args.table and name != args.table:
            continue
        sql = t.table_sql(name)
        print(f"-- ===== {name} ({'complete' if r.complete else 'incomplete'})")
        if r.files:
            print(f"-- placeholder file inputs (here or in tables read): {', '.join(r.files)}")
        if r.unresolved:
            print(f"-- unresolved variables (here or in tables read): {', '.join(r.unresolved)}")
        for u in r.untranslated:
            print(f"-- not translated: statement {u.statement} ({u.span}): {u.reason} [{u.tier}]")
        if sql:
            print(sql + ";\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    d = Path(args.app_dir)
    log, app = _load(d)
    t = compose(app, connections=_connections(d))  # type: ignore[arg-type]
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
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adce", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("parse", cmd_parse), ("translate", cmd_translate), ("validate", cmd_validate)):
        sp = sub.add_parser(name)
        sp.add_argument("app_dir")
        if name == "translate":
            sp.add_argument("--table")
        sp.set_defaults(fn=fn)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
