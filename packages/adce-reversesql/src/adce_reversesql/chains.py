"""QVD chains across applications (ADR-Q4 §3, TD §9.3).

Qlik applications pass data through QVD files: one application STOREs a table to a QVD, others LOAD it. In Dremio
there is no QVD, so the consumer's SQL must compute what the producer stored. This module:

- `produced_facts`: what an application's translation stores, as `qvd_produced` facts (subject `qvd:<file>`, value:
  the producer app, table, path and the SQL of the stored version). Agent 20 proposes them after each run.
- `known_producers`: the producers the knowledge has for each QVD file (another application's facts).
- `choose`: one producer per QVD. With several, the one the QVD header names as its creator (`CreatorDoc`) or a
  reviewer confirmed (`qvd_linkage`); otherwise the QVD is ambiguous.
- `inline_producers`: replaces each `"__qvd__"."<file>"` in a consumer's SQL by the producer's CTE chain (its CTEs,
  renamed under the producer's name, and a CTE `qvd__<file>` for the stored table). The producer's SQL can read QVDs
  of a third application; those are inlined too, up to MAX_DEPTH, so the final SQL reads only database sources.
  A QVD without a producer stays a placeholder and is reported, with the creator the header names when there is one.
- `order_apps`: applications ordered producers first, for a run over several applications.

QVDs are matched by file name (case-insensitive, without the folder): `lib://QVD/Orders.qvd` and
`lib://Shared/orders.qvd` are the same file to Qlik when the folders point at the same place, which is the usual
set-up; a reviewer can override a wrong match with a `resolves_to` mapping on the QVD (mappings win).

Local validation is unchanged: a consumer is still compared with the QVD contents it was given. Inlining is for the
SQL that runs in Dremio (agent 24's SQL and the pack).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .compose import DIALECT, QVD_DB

MAX_DEPTH = 5


def qvd_key(path: str) -> str:
    """The file name of a QVD path, lower case: what producers and consumers are matched on."""
    return path.replace("\\", "/").strip().strip("'\"[]").rsplit("/", 1)[-1].lower()


def _ident(s: str) -> str:
    out = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower() or "x"
    return out if out[0].isalpha() else f"x_{out}"


@dataclass
class Producer:
    app_id: str
    table: str
    path: str
    sql: str
    complete: bool = True


@dataclass
class Inlined:
    sql: str
    inlined: list[dict[str, Any]] = field(default_factory=list)  # {qvd, app_id, table, depth}
    missing: list[str] = field(default_factory=list)  # QVD files read with no producer
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # QVD file -> producer apps


def produced_facts(app_id: str, translation: Any) -> list[dict[str, Any]]:
    """The QVDs an application's translation stores: (subject, value) for qvd_produced facts."""
    out = []
    for name, r in sorted(translation.tables.items()):
        for target, cte in r.stores:
            if cte not in translation.ctes:
                continue
            out.append({"subject": f"qvd:{qvd_key(target)}",
                        "value": {"app_id": app_id, "table": name, "path": target, "complete": r.complete,
                                  "sql": translation.render(cte, DIALECT, comments=False)}})
    return out


async def known_producers(kg: Any, consumer: str | None = None) -> dict[str, list[Producer]]:
    """QVD file -> its producers in other applications (the latest fact per application and file)."""
    by: dict[tuple[str, str], Producer] = {}
    for f in await kg.facts(kind="qvd_produced"):
        v = f.value
        if v.get("app_id") == consumer or not v.get("sql"):
            continue
        by[(f.subject.removeprefix("qvd:"), v["app_id"])] = Producer(v["app_id"], v.get("table", ""),
                                                                    v.get("path", ""), v["sql"],
                                                                    bool(v.get("complete", True)))
    out: dict[str, list[Producer]] = {}
    for (key, _), p in sorted(by.items()):
        out.setdefault(key, []).append(p)
    return out


def _doc_name(creator: str) -> str:
    """'C:\\Qlik\\Sales Extract.qvw' -> 'sales extract': the document a QVD header names, compared to app ids."""
    base = creator.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(qvw|qvf)$", "", base, flags=re.I).strip().lower()


def choose(producers: dict[str, list[Producer]], creators: dict[str, str] | None = None,
           confirmed: dict[str, str] | None = None) -> tuple[dict[str, Producer], dict[str, list[str]]]:
    """One producer per QVD file. creators: QVD file -> the CreatorDoc of its header; confirmed: QVD file -> the
    producer application a reviewer confirmed. Returns (chosen, ambiguous: file -> candidate apps)."""
    chosen: dict[str, Producer] = {}
    ambiguous: dict[str, list[str]] = {}
    for key, ps in producers.items():
        if len(ps) == 1:
            chosen[key] = ps[0]
            continue
        want = (confirmed or {}).get(key)
        if want is None and (creators or {}).get(key):
            doc = _doc_name(creators[key])  # type: ignore[index]
            want = next((p.app_id for p in ps if _ident(p.app_id) == _ident(doc)), None)
        pick = next((p for p in ps if p.app_id == want), None)
        if pick:
            chosen[key] = pick
        else:
            ambiguous[key] = sorted(p.app_id for p in ps)
    return chosen, ambiguous


def _qvd_tables(tree: exp.Expr) -> list[exp.Table]:
    return [t for t in tree.find_all(exp.Table) if t.db == QVD_DB]


def inline_producers(sql: str, producers: dict[str, Producer], ambiguous: dict[str, list[str]] | None = None,
                     mapped: set[str] | None = None) -> Inlined:
    """The consumer's SQL with every QVD that has a producer replaced by the producer's CTE chain.
    mapped: QVD subjects (`qvd:<path>`) a reviewer mapped to a dataset; those are left for apply_mappings."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    out = Inlined(sql)
    added: dict[str, exp.CTE] = {}  # CTE name -> definition, in dependency order
    seen_missing: set[str] = set()

    def expand(node: exp.Expr, depth: int, chain: tuple[str, ...]) -> None:
        for t in _qvd_tables(node):
            key = qvd_key(t.name)
            if mapped and f"qvd:{t.name}" in mapped:
                continue
            p = producers.get(key)
            # no producer, a cycle (the chain reads a QVD it is producing) or too deep: stays a placeholder
            if p is None or key in chain or depth >= MAX_DEPTH:
                if key not in seen_missing:
                    seen_missing.add(key)
                    if p is None and ambiguous and key in ambiguous:
                        out.ambiguous[key] = ambiguous[key]
                    else:
                        out.missing.append(key)
                continue
            target = f"qvd__{_ident(key.removesuffix('.qvd'))}"
            if target not in added:
                ptree = sqlglot.parse_one(p.sql, read=DIALECT)
                prefix = f"{_ident(p.app_id)}__"
                with_ = ptree.args.get("with_")
                pctes = list(with_.expressions) if with_ else []
                names = {c.alias_or_name for c in pctes}
                for tbl in ptree.find_all(exp.Table):  # the producer's own CTEs, renamed under its name
                    if not tbl.db and tbl.name in names:
                        tbl.set("this", exp.to_identifier(prefix + tbl.name))
                for c in pctes:
                    c.set("alias", exp.TableAlias(this=exp.to_identifier(prefix + c.alias_or_name)))
                ptree.set("with_", None)
                expand(ptree, depth + 1, (*chain, key))  # a QVD the producer reads from a third application
                for c in pctes:
                    expand(c, depth + 1, (*chain, key))
                    added.setdefault(c.alias_or_name, c)
                added[target] = exp.CTE(this=ptree, alias=exp.TableAlias(this=exp.to_identifier(target)))
                out.inlined.append({"qvd": key, "app_id": p.app_id, "table": p.table, "depth": depth,
                                    "complete": p.complete})
            t.replace(exp.Table(this=exp.to_identifier(target), alias=t.args.get("alias")))

    expand(tree, 0, ())
    if added:
        with_ = tree.args.get("with_")
        own = list(with_.expressions) if with_ else []
        tree.set("with_", exp.With(expressions=[*added.values(), *own]))
        out.sql = tree.sql(dialect=DIALECT, pretty=True)
    return out


def order_apps(apps: list[str], produced: dict[str, set[str]], reads: dict[str, set[str]]) -> list[str]:
    """Producers before consumers. produced / reads: app -> QVD files it stores / reads. Cycles keep the given
    order for the applications in them."""
    makers: dict[str, set[str]] = {}
    for a, qs in produced.items():
        for q in qs:
            makers.setdefault(q, set()).add(a)
    deps = {a: {m for q in reads.get(a, set()) for m in makers.get(q, set()) if m != a and m in apps} for a in apps}
    done: list[str] = []
    visiting: set[str] = set()

    def visit(a: str) -> None:
        if a in done or a in visiting:
            return
        visiting.add(a)
        for d in sorted(deps[a], key=apps.index):
            visit(d)
        visiting.discard(a)
        done.append(a)

    for a in apps:
        visit(a)
    return done
