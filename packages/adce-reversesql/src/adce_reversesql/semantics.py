"""Query-checker rules for Qlik semantics (agent 25), checked on the SQL itself, independently of how it was built.

Charts (Architecture §5.5, BI semantics rules; CP-001 §3):
- fan_out (blocking): Qlik aggregates a field once per record of the table that holds it, however many records of
  other tables are associated; a SQL join before an aggregate repeats records (double counting). An aggregate may
  read one relation, or rows joined to a DISTINCT bridge on a row id (the grain-aware shape charts.py builds); any
  other aggregate over a join is rejected.
- set_fields (blocking): every field a set modifier names exists in the data model (otherwise Qlik ignores nothing
  and the SQL would filter on a field that is not there).
- distinct_across_tables (warning): Count(DISTINCT F) where F is held by several associated tables: Qlik counts the
  field's values across all of them; the SQL counts one table's.
- totals (warning): TOTAL or Aggr in a measure: scope rules apply (TOTAL <F> keeps F; Aggr is T3).

Script tables:
- join_fields (warning): a JOIN or KEEP whose two sides share no field is a cross join in Qlik (and in the SQL),
  almost always a script mistake worth a look.

Dual values and NULL behaviour are reproduced by the expression translator (expressions.py) rather than checked here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from adce_core.bi_ir import BIApp
from adce_qlik.load_parser import parse_expression
from sqlglot import exp

from .compose import DIALECT, Translation

AGGS = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)


@dataclass
class SemanticRule:
    name: str
    ok: bool
    message: str = ""
    blocking: bool = True


def _walk(e: Any) -> Any:
    yield e
    for a in e.args:
        yield from _walk(a)


def _from_parts(sel: exp.Select) -> list[Any]:
    out = []
    f = sel.args.get("from_") or sel.args.get("from")
    if f is not None:
        out.append(f.this)
    out += [j.this for j in sel.args.get("joins") or []]
    return out


def _is_bridge(node: Any) -> bool:
    """A DISTINCT subquery exposing a row id: the grain-aware bridge (each measure record once per dimension)."""
    q = node.this if isinstance(node, exp.Subquery) else None
    return isinstance(q, exp.Select) and bool(q.args.get("distinct")) and any(
        e.alias_or_name == "__rid" for e in q.expressions)


def _joined_on_rid(sel: exp.Select) -> bool:
    for j in sel.args.get("joins") or []:
        on = j.args.get("on")
        if on is None or not all(c.name == "__rid" for c in on.find_all(exp.Column)):
            return False
    return True


def fan_out(sql: str) -> list[str]:
    """Aggregates computed over a join that can repeat the aggregated records. Empty when the SQL is grain-safe."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    problems = []
    for agg in tree.find_all(*AGGS):
        if agg.find_ancestor(exp.Window) is not None:
            continue
        sel = agg.find_ancestor(exp.Select)
        if sel is None or not sel.args.get("joins"):
            continue  # reads one relation
        parts = _from_parts(sel)
        if any(_is_bridge(p) for p in parts) and _joined_on_rid(sel):
            continue  # each record once, through the DISTINCT row-id bridge
        problems.append(f"{agg.sql(dialect=DIALECT)[:80]} is computed over a join "
                        f"({', '.join(p.alias_or_name for p in parts)}): associated records repeat it")
    return problems


def chart_rules(app: BIApp, t: Translation, visual_id: str, sql: str | None) -> list[SemanticRule]:
    from .charts import DataModel

    v = next((x for x in app.visuals if x.id == visual_id), None)
    rules: list[SemanticRule] = []
    if v is None or sql is None:
        return rules
    model = DataModel(app, t)
    bad = fan_out(sql)
    rules.append(SemanticRule("fan_out", not bad, "; ".join(bad[:3])))
    unknown, distinct_multi, scoped = [], [], []
    for m in v.measures:
        try:
            e = parse_expression(m.expression)
        except Exception:  # noqa: BLE001 - not parsed: the translator reported it already
            continue
        for node in _walk(e):
            if node.kind != "call":
                continue
            if node.set_expr:
                from adce_qlik.set_analysis import parse_set

                try:
                    s = parse_set(node.set_expr)
                except Exception:  # noqa: BLE001
                    continue
                unknown += [mod.field for mod in s.modifiers if not model.owners(mod.field)]
            words = (node.modifier or "").split()
            if (node.name or "").lower() == "count" and "DISTINCT" in words:
                for f in node.args[0].fields() if node.args else []:
                    owners = model.owners(f)
                    if len(owners) > 1:
                        distinct_multi.append(f"{f} ({', '.join(sorted(owners))})")
            if "TOTAL" in words or (node.name or "").lower() == "aggr":
                scoped.append(node.text[:60])
    rules.append(SemanticRule("set_fields", not unknown, f"set modifiers name fields not in the data model: "
                              f"{', '.join(sorted(set(unknown)))}" if unknown else ""))
    rules.append(SemanticRule("distinct_across_tables", not distinct_multi,
                              "Count(DISTINCT) of a field several tables hold (Qlik counts across all of them): "
                              + ", ".join(sorted(set(distinct_multi))) if distinct_multi else "", blocking=False))
    rules.append(SemanticRule("totals", not scoped, "TOTAL / Aggr scope to review: " + "; ".join(scoped[:3])
                              if scoped else "", blocking=False))
    return rules


def script_rules(app: BIApp, table: str) -> list[SemanticRule]:
    qt = app.tables.get(table)
    if qt is None:
        return []
    cross = []
    have: set[str] = set()
    for step in qt.steps:
        action = step.action.value
        if action in ("join", "keep") and have and not (have & set(step.fields)):
            cross.append(f"{action} at {step.ops[0].span.file}:{step.ops[0].span.start_line}" if step.ops else action)
        if action == "keep" and step.other_table and step.other_table in app.tables:
            other = set(app.tables[step.other_table].fields)
            if other and not (other & set(step.fields)):
                cross.append(f"keep against {step.other_table}")
        have |= set(step.fields)
    return [SemanticRule("join_fields", not cross, "no shared field, so Qlik makes a cartesian product: "
                         + "; ".join(cross) if cross else "", blocking=False)]
