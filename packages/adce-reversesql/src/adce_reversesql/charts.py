"""Charts and tables → Dremio SQL, with Qlik's aggregation semantics (agents 23 join planner and 29 expression
translator).

The data model is the script's final tables; Qlik associates tables through fields with the same name. A chart
groups by its dimensions and evaluates each measure over the records associated with each dimension value.

Qlik semantics reproduced here:
- An aggregation counts every record of the table holding its fields once per dimension value, however many
  records of other tables link the two. A plain join would multiply it (fan-out); here each record gets a row id,
  a DISTINCT bridge links row ids to dimension values, and the aggregation reads each record once.
- Records without a match in the dimension's table count under a null dimension value (outer association).
- Set analysis {<F={…}, G-={…}>} becomes filters on the associated records; {1} and {$} are the same in an export
  without selections.
- TOTAL ignores the chart's dimensions (TOTAL <F> keeps F); arithmetic between aggregations, If(), division by zero
  → NULL follow the script translator.
- Rows whose measures are all zero or null are hidden, as in Qlik's default (suppress zero values).
Not reproduced (reported, never guessed): Aggr() and nested aggregations, calculated dimensions, aggregations over
fields of several tables, set operators, P()/E(), alternate states, and models with circular references.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from adce_core.bi_ir import BIApp, Expr, Visual
from adce_qlik.load_parser import LoadParseError, parse_expression
from adce_qlik.set_analysis import SetExpression, SetParseError, parse_set
from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

from .compose import DIALECT, Translation
from .expressions import ExprContext, Translator, Untranslatable, is_aggregate

AGG_FUNCS = {"sum", "count", "avg", "min", "max", "only"}
NUMERIC_AGGS = {"sum", "count", "avg", "min", "max"}


# ---------------------------------------------------------------------------------------------- data model
@dataclass
class Association:
    a: str
    b: str
    keys: list[str]


@dataclass
class JoinPlan:
    """A join tree rooted at one table: edges in breadth-first order, each (parent, child, keys)."""

    root: str
    edges: list[tuple[str, str, list[str]]] = field(default_factory=list)

    @property
    def tables(self) -> list[str]:
        return [self.root, *[c for _, c, _ in self.edges]]


class DataModel:
    """The final tables of the script and their associations (shared field names)."""

    def __init__(self, app: BIApp, t: Translation) -> None:
        self.t = t
        self.fields: dict[str, list[str]] = {}
        self.cte: dict[str, str] = {}
        self.untranslated: list[str] = []
        for name, qt in app.tables.items():
            if qt.mapping or qt.dropped_at is not None:
                continue
            r = t.tables.get(name)
            if r is None or r.final_cte is None or not r.complete:
                self.untranslated.append(name)
                continue
            self.cte[name] = r.final_cte
            self.fields[name] = list(t.ctes[r.final_cte].columns or qt.fields)
        names = list(self.fields)
        self.associations = [Association(a, b, [f for f in self.fields[a] if f in self.fields[b]])
                             for i, a in enumerate(names) for b in names[i + 1:]]
        self.associations = [x for x in self.associations if x.keys]

    def owners(self, f: str) -> list[str]:
        return [n for n, fs in self.fields.items() if f in fs]

    def neighbours(self, table: str) -> list[tuple[str, list[str]]]:
        out = []
        for x in self.associations:
            if x.a == table:
                out.append((x.b, x.keys))
            elif x.b == table:
                out.append((x.a, x.keys))
        return out

    def synthetic_keys(self) -> list[Association]:
        return [x for x in self.associations if len(x.keys) > 1]

    def has_cycle(self) -> bool:
        parent = {n: n for n in self.fields}

        def find(n: str) -> str:
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        for x in self.associations:
            ra, rb = find(x.a), find(x.b)
            if ra == rb:
                return True
            parent[ra] = rb
        return False

    def plan(self, root: str, needed: list[str]) -> JoinPlan:
        """Agent 23: the smallest join tree from root reaching every needed table (shortest paths, breadth-first)."""
        prev: dict[str, tuple[str, list[str]] | None] = {root: None}
        q = deque([root])
        while q:
            cur = q.popleft()
            for nb, keys in self.neighbours(cur):
                if nb not in prev:
                    prev[nb] = (cur, keys)
                    q.append(nb)
        plan = JoinPlan(root=root)
        included = {root}
        for target in needed:
            if target not in prev:
                raise Untranslatable(f"table {target} is not associated with {root}", "T2")
            path: list[tuple[str, str, list[str]]] = []
            n = target
            while n not in included:
                step = prev[n]
                assert step is not None
                path.append((step[0], n, step[1]))
                n = step[0]
            for edge in reversed(path):
                plan.edges.append(edge)
                included.add(edge[1])
        return plan

    def nearest_owner(self, f: str, root: str) -> str:
        owners = self.owners(f)
        if not owners:
            raise Untranslatable(f"field {f} is not in the data model", "T1")
        if root in owners:
            return root
        dist = {root: 0}
        q = deque([root])
        while q:
            cur = q.popleft()
            for nb, _ in self.neighbours(cur):
                if nb not in dist:
                    dist[nb] = dist[cur] + 1
                    q.append(nb)
        reachable = [o for o in owners if o in dist]
        if not reachable:
            raise Untranslatable(f"field {f} is in no table associated with {root}", "T2")
        return min(reachable, key=lambda o: (dist[o], owners.index(o)))


# ---------------------------------------------------------------------------------------------- chart SQL
@dataclass
class ChartSQL:
    visual_id: str
    title: str
    dimensions: list[str]  # output column labels
    measures: list[str]
    sql: str | None = None  # Dremio SQL returning dimensions then measures
    untranslated: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)  # model tables the chart reads
    tier: str = "T1"

    @property
    def complete(self) -> bool:
        return self.sql is not None and not self.untranslated


@dataclass
class _Agg:
    name: str  # CTE name
    node: Expr
    func: str
    distinct: bool
    table: str
    set_expr: SetExpression | None
    total: bool
    total_dims: list[str] | None


def _q(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


class ChartTranslator:
    def __init__(self, app: BIApp, t: Translation) -> None:
        self.app = app
        self.t = t
        self.model = DataModel(app, t)
        self.variables = {k: v.value for k, v in app.variables.items() if v.value is not None}

    def model_notes(self) -> list[str]:
        notes = [f"synthetic key between {x.a} and {x.b} on {', '.join(x.keys)}: joined on all of them"
                 for x in self.model.synthetic_keys()]
        if self.model.has_cycle():
            notes.append("circular reference in the data model: Qlik loosens one table; charts over it need review")
        return notes

    def check_measure(self, expression: str) -> tuple[bool, str, str, list[str]]:
        """Can this measure be translated? Returns (ok, reason, tier, model tables it reads)."""
        try:
            aggs: list[_Agg] = []
            self._collect(parse_expression(expression), aggs)
            if not aggs:
                raise Untranslatable("measure without an aggregation", "T2")
            return True, "", "T1", sorted({a.table for a in aggs})
        except Untranslatable as e:
            return False, str(e), e.tier, []
        except (LoadParseError, SetParseError) as e:
            return False, f"expression not understood: {e}", "T2", []

    def visual(self, v: Visual, dims_override: list[str] | None = None) -> ChartSQL:
        dims = [d.field for d in v.dimensions] if dims_override is None else dims_override
        out = ChartSQL(visual_id=v.id, title=v.title, dimensions=[d.label for d in v.dimensions]
                       if dims_override is None else list(dims_override), measures=[m.label for m in v.measures])
        try:
            if self.model.untranslated:
                out.notes.append(f"tables not translated: {', '.join(self.model.untranslated)}")
            if self.model.has_cycle():
                raise Untranslatable("circular reference in the data model", "T3")
            for d in dims:
                if d.startswith("="):
                    raise Untranslatable(f"calculated dimension {d}", "T2")
                if not self.model.owners(d):
                    raise Untranslatable(f"dimension field {d} is not in the data model", "T1")
            parts, ctes = self._build(dims, [m.expression for m in v.measures], out)
            out.sql = "WITH " + ",\n".join(self.t.cte_parts(sorted({self.model.cte[n] for n in out.tables}))
                                           + ctes) + "\n" + parts
        except Untranslatable as e:
            out.untranslated.append(str(e))
            out.tier = e.tier
        except (LoadParseError, SetParseError) as e:
            out.untranslated.append(f"expression not understood: {e}")
            out.tier = "T2"
        return out

    # ------------------------------------------------------------------ building
    def _build(self, dims: list[str], expressions: list[str], out: ChartSQL) -> tuple[str, list[str]]:
        aggs: list[_Agg] = []
        rewritten: list[Expr] = []
        numeric: list[bool] = []
        for text in expressions:
            e = parse_expression(text)
            rewritten.append(self._collect(e, aggs))
            numeric.append(self._is_numeric(e))
        if not aggs:
            raise Untranslatable("measure without an aggregation", "T2")
        ctes: list[str] = []
        tables: set[str] = set()
        for a in aggs:
            body, used = self._agg_cte(a, dims)
            tables |= used
            ctes.append(f"{a.name} AS (\n{_indent(body.sql(dialect=DIALECT, pretty=True))}\n)")
        out.tables = sorted(tables)
        dim_cols = [f"d{i + 1}" for i in range(len(dims))]
        grouped = [a for a in aggs if not a.total]
        if dims and not grouped:
            raise Untranslatable("every measure uses TOTAL: no rows to show", "T2")
        for a in aggs:
            if a.total and a.total_dims and any(f not in dims for f in a.total_dims):
                raise Untranslatable(f"TOTAL <{', '.join(a.total_dims)}> names a field that is not a dimension", "T2")
        sel: list[SqlExpr] = []
        if dims:
            universe = None
            for a in grouped:
                s = exp.select(*[exp.column(c, quoted=True) for c in dim_cols]).from_(a.name)
                universe = s if universe is None else exp.union(universe, s, distinct=True)
            ctes.append(f"chart_rows AS (\n{_indent(universe.sql(dialect=DIALECT, pretty=True))}\n)")  # type: ignore[union-attr]
            q = exp.select(*[exp.alias_(exp.column(c, table="r", quoted=True), lbl, quoted=True)
                             for c, lbl in zip(dim_cols, out.dimensions, strict=True)]).from_(
                exp.Table(this=exp.to_identifier("chart_rows"), alias=exp.TableAlias(this=exp.to_identifier("r"))))
        else:
            q = exp.select().from_(exp.Table(this=exp.to_identifier(aggs[0].name),
                                             alias=exp.TableAlias(this=exp.to_identifier(aggs[0].name))))
        for a in aggs:
            if not dims and a is aggs[0]:
                continue
            on_cols = [] if (a.total and not a.total_dims) else (
                [dim_cols[dims.index(f)] for f in (a.total_dims or [])] if a.total else dim_cols)
            tbl = exp.Table(this=exp.to_identifier(a.name), alias=exp.TableAlias(this=exp.to_identifier(a.name)))
            if not on_cols or not dims:
                q = q.join(tbl, join_type="cross")
            else:
                cond = exp.and_(*[exp.NullSafeEQ(this=exp.column(c, table=a.name, quoted=True),
                                                 expression=exp.column(c, table="r", quoted=True)) for c in on_cols])
                q = q.join(tbl, on=cond, join_type="left")
        values = []
        funcs = {a.name: a.func for a in aggs}
        for e, lbl in zip(rewritten, out.measures, strict=True):
            v = self._value(e, funcs, dim_cols if dims else None)
            values.append(v)
            sel.append(exp.alias_(v, lbl, quoted=True))
        q = q.select(*sel)
        if any(v.find(exp.Window) for v in values):
            out.notes.append("Above(): rows in dimension order, ascending (the chart's sort is not known); measures "
                             "over earlier rows do not hide a row when they are zero")
            out.tier = "T2"
        if dims and not all(v.find(exp.Window) for v in values):
            empties = []
            for v, num in zip(values, numeric, strict=True):
                if v.find(exp.Window):
                    continue  # a window cannot be filtered on in the same query; Qlik hides on the others too
                c = v.copy()
                empties.append(exp.or_(exp.Is(this=c, expression=exp.Null()),
                                       exp.EQ(this=v.copy(), expression=exp.Literal.number(0))) if num
                               else exp.Is(this=c, expression=exp.Null()))
            q = q.where(exp.Not(this=exp.Paren(this=exp.and_(*empties))))
        return q.sql(dialect=DIALECT, pretty=True), ctes

    def _collect(self, e: Expr, aggs: list[_Agg]) -> Expr:
        """Replace each aggregation by a reference to its CTE's value; returns the rewritten expression."""
        if e.kind == "call" and (e.name or "").lower() in AGG_FUNCS:
            fn = (e.name or "").lower()
            if fn not in AGG_FUNCS or len(e.args) != 1:
                raise Untranslatable(f"{e.name}() with {len(e.args)} arguments", "T2")
            if any(is_aggregate(a) for a in e.args) or "aggr" in e.text.lower():
                raise Untranslatable(f"nested aggregation or Aggr() in {e.text}", "T3")
            fields = e.args[0].fields()
            if not fields:
                raise Untranslatable(f"{e.text}: aggregation without fields", "T2")
            tables = [t for t in self.model.fields if all(f in self.model.fields[t] for f in fields)]
            if not tables:
                raise Untranslatable(f"{e.text}: fields from several tables in one aggregation", "T2")
            words = (e.modifier or "").split()
            a = _Agg(name=f"agg_{len(aggs) + 1}", node=e, func=fn, distinct="DISTINCT" in words, table=tables[0],
                     set_expr=parse_set(e.set_expr) if e.set_expr else None, total="TOTAL" in words,
                     total_dims=e.total_dims)
            aggs.append(a)
            return Expr(kind="field", name=f"__{a.name}", text=e.text)
        if e.kind == "call" and (e.name or "").lower() == "aggr":
            raise Untranslatable("Aggr()", "T3")
        if e.kind == "field":
            raise Untranslatable(f"field {e.name} outside an aggregation in a measure", "T2")
        return e.model_copy(update={"args": [self._collect(x, aggs) for x in e.args]})

    @staticmethod
    def _is_numeric(e: Expr) -> bool:
        if e.kind == "call":
            fn = (e.name or "").lower()
            return fn in NUMERIC_AGGS or fn in ("if", "round", "fabs", "rangesum", "num", "above")
        return e.kind in ("binop", "unop", "number")

    def _value(self, e: Expr, funcs: dict[str, str], dim_cols: list[str] | None = None) -> SqlExpr:
        """The measure's SQL over the chart's rows. Above(x [, n]) and RangeSum(Above(x, 0, n)) become window
        functions over the rows in dimension order (ascending: the export's sort is not known)."""
        order = exp.Order(expressions=[exp.Ordered(this=exp.column(c, table="r", quoted=True))
                                       for c in (dim_cols or [])])
        windows: dict[str, SqlExpr] = {}

        def lift(x: Expr) -> Expr:
            name = (x.name or "").lower() if x.kind == "call" else ""
            if name == "rangesum" and len(x.args) == 1 and (x.args[0].name or "").lower() == "above":
                inner, off, n = _above_args(x.args[0])
                if off != 0 or n is None:
                    raise Untranslatable("RangeSum(Above()) that does not start at the current row", "T2")
                key = f"__w{len(windows) + 1}"
                windows[key] = exp.Window(this=exp.Sum(this=plain(lift(inner))), order=order.copy(),
                                          spec=exp.WindowSpec(kind="ROWS", start=exp.Literal.number(n - 1),
                                                              start_side="PRECEDING", end="CURRENT ROW"))
                return Expr(kind="field", name=key, text=x.text)
            if name == "above":
                inner, off, n = _above_args(x)
                if n not in (None, 1):
                    raise Untranslatable("Above() over a range of rows outside RangeSum()", "T2")
                key = f"__w{len(windows) + 1}"
                lag = exp.Lag(this=plain(lift(inner)), offset=exp.Literal.number(off)) if off != 1 else \
                    exp.Lag(this=plain(lift(inner)))
                windows[key] = exp.Window(this=lag, order=order.copy())
                return Expr(kind="field", name=key, text=x.text)
            return x.model_copy(update={"args": [lift(a) for a in x.args]})

        def plain(x: Expr) -> SqlExpr:
            tr = Translator(ExprContext(alias="__x", columns=None, mappings={}, variables=self.variables))
            return tr.value(x).transform(fix)

        def fix(n: SqlExpr) -> SqlExpr:
            if isinstance(n, exp.Column) and n.table == "__x" and n.name.startswith("__agg_"):
                col = exp.column("v", table=n.name[2:], quoted=True)
                # Qlik: Sum() and Count() over no records are 0; Avg, Min, Max and Only are null
                if funcs[n.name[2:]] in ("sum", "count"):
                    return exp.Coalesce(this=col, expressions=[exp.Literal.number(0)])
                return col
            if isinstance(n, exp.Column) and n.table == "__x" and n.name in windows:
                return windows[n.name].copy()
            return n

        return plain(lift(e))

    def _agg_cte(self, a: _Agg, dims: list[str]) -> tuple[exp.Select, set[str]]:
        root = a.table
        group: list[str] = [] if (a.total and not a.total_dims) else list((a.total_dims or []) if a.total else dims)
        filters = [m for m in (a.set_expr.modifiers if a.set_expr else [])]
        owner = {f: self.model.nearest_owner(f, root) for f in [*group, *[m.field for m in filters]]}
        plan = self.model.plan(root, sorted(set(owner.values()) - {root}))
        if not plan.edges:  # one table: no other table can repeat its records, a plain GROUP BY is exact
            return self._single_table(a, root, group, filters), {root}
        alias = {tname: f"t{i}" for i, tname in enumerate(plan.tables)}
        # m: the measure table with a row id (ties are identical rows, so any numbering among them is equivalent)
        m_cols = self.model.fields[root]
        rid = exp.Window(this=exp.RowNumber(), order=exp.Order(expressions=[
            exp.Ordered(this=exp.column(c, quoted=True), nulls_first=True) for c in m_cols]))
        m_body = exp.select(exp.alias_(rid, "__rid", quoted=True),
                            *[exp.column(c, quoted=True) for c in m_cols]).from_(self.model.cte[root])
        bridge = exp.select(*[exp.alias_(exp.column(f, table=alias[owner[f]], quoted=True), f"d{i + 1}", quoted=True)
                              for i, f in enumerate(group)],
                            exp.column("__rid", table="t0", quoted=True)).distinct().from_(
            exp.Subquery(this=m_body, alias=exp.TableAlias(this=exp.to_identifier("t0"))))
        for parent, child, keys in plan.edges:
            cond = exp.and_(*[exp.EQ(this=exp.column(k, table=alias[parent], quoted=True),
                                     expression=exp.column(k, table=alias[child], quoted=True)) for k in keys])
            bridge = bridge.join(exp.Table(this=exp.to_identifier(self.model.cte[child]),
                                           alias=exp.TableAlias(this=exp.to_identifier(alias[child]))),
                                 on=cond, join_type="left")
        conds = [self._filter(m.field, m.op, m.values, exp.column(m.field, table=alias[owner[m.field]], quoted=True))
                 for m in filters]
        conds = [c for c in conds if c is not None]
        if conds:
            bridge = bridge.where(exp.and_(*conds))
        tr = Translator(ExprContext(alias="m", columns=m_cols, mappings={}, variables=self.variables))
        inner = tr.value(a.node.args[0])
        agg = self._agg_sql(a, inner)
        body = exp.select(*[exp.column(f"d{i + 1}", table="b", quoted=True) for i in range(len(group))],
                          exp.alias_(agg, "v", quoted=True)).from_(
            exp.Subquery(this=bridge, alias=exp.TableAlias(this=exp.to_identifier("b")))).join(
            exp.Subquery(this=m_body.copy(), alias=exp.TableAlias(this=exp.to_identifier("m"))),
            on=exp.EQ(this=exp.column("__rid", table="m", quoted=True),
                      expression=exp.column("__rid", table="b", quoted=True)), join_type="inner")
        if group:
            body = body.group_by(*[exp.column(f"d{i + 1}", table="b", quoted=True) for i in range(len(group))])
        return body, set(plan.tables)

    def _single_table(self, a: _Agg, root: str, group: list[str], filters: list) -> exp.Select:  # type: ignore[type-arg]
        m_cols = self.model.fields[root]
        tr = Translator(ExprContext(alias="m", columns=m_cols, mappings={}, variables=self.variables))
        body = exp.select(*[exp.alias_(exp.column(f, table="m", quoted=True), f"d{i + 1}", quoted=True)
                            for i, f in enumerate(group)],
                          exp.alias_(self._agg_sql(a, tr.value(a.node.args[0])), "v", quoted=True)).from_(
            exp.Table(this=exp.to_identifier(self.model.cte[root]), alias=exp.TableAlias(this=exp.to_identifier("m"))))
        conds = [self._filter(m.field, m.op, m.values, exp.column(m.field, table="m", quoted=True)) for m in filters]
        conds = [c for c in conds if c is not None]
        if conds:
            body = body.where(exp.and_(*conds))
        if group:
            body = body.group_by(*[exp.column(f, table="m", quoted=True) for f in group])
        return body

    @staticmethod
    def _agg_sql(a: _Agg, inner: SqlExpr) -> SqlExpr:
        arg = exp.Distinct(expressions=[inner]) if a.distinct else inner
        if a.func == "sum":
            return exp.Sum(this=arg)
        if a.func == "avg":
            return exp.Avg(this=arg)
        if a.func == "min":
            return exp.Min(this=arg)
        if a.func == "max":
            return exp.Max(this=arg)
        if a.func == "count":
            return exp.Count(this=arg)
        # Only(): the value when exactly one distinct value exists, otherwise NULL
        one = exp.EQ(this=exp.Count(this=exp.Distinct(expressions=[inner.copy()])), expression=exp.Literal.number(1))
        return exp.Case(ifs=[exp.If(this=one, true=exp.Min(this=inner.copy()))])

    @staticmethod
    def _filter(field_name: str, op: str, values: list, col: SqlExpr) -> SqlExpr | None:  # type: ignore[type-arg]
        if op in ("=", "*=") and not values:
            return None  # F= clears the field: no restriction
        if op not in ("=", "*=", "-="):
            raise Untranslatable(f"set modifier {field_name}{op} (needs current selections)", "T2")
        parts: list[SqlExpr] = []
        literals = [v.value for v in values if v.kind == "literal"]
        if literals:
            parts.append(exp.In(this=col.copy(), expressions=[
                exp.Literal.number(x) if isinstance(x, int | float) else exp.Literal.string(str(x)) for x in literals]))
        for v in values:
            if v.kind != "search":
                continue
            s = str(v.value)
            cmps = re.findall(r"(>=|<=|<>|>|<|=)\s*([^<>=]+)", s)
            if cmps and re.match(r"^\s*(>=|<=|<>|>|<|=)", s):
                cls = {">=": exp.GTE, "<=": exp.LTE, ">": exp.GT, "<": exp.LT, "=": exp.EQ, "<>": exp.NEQ}
                terms = []
                for o, val in cmps:
                    val = val.strip()
                    lit = exp.Literal.number(val) if re.fullmatch(r"-?\d+(\.\d+)?", val) else exp.Literal.string(val)
                    terms.append(cls[o](this=col.copy(), expression=lit))
                parts.append(exp.and_(*terms))
            else:
                pat = s.replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
                parts.append(exp.ILike(this=exp.Cast(this=col.copy(), to=exp.DataType.build("VARCHAR")),
                                       expression=exp.Literal.string(pat)))
        cond = exp.or_(*parts) if len(parts) > 1 else parts[0] if parts else exp.false()
        return exp.Not(this=exp.Paren(this=cond)) if op == "-=" else cond


def _above_args(e: Expr) -> tuple[Expr, int, int | None]:
    """Above(expr [, offset [, count]]): (expr, offset, count); offset and count must be literal numbers."""
    if not e.args or len(e.args) > 3:
        raise Untranslatable("Above() with no or too many arguments", "T2")
    nums = []
    for a in e.args[1:]:
        if a.kind != "number" or not isinstance(a.value, int):
            raise Untranslatable("Above() with a non-literal offset or count", "T2")
        nums.append(a.value)
    return e.args[0], (nums[0] if nums else 1), (nums[1] if len(nums) > 1 else None)


def _indent(s: str) -> str:
    return "\n".join("  " + ln for ln in s.splitlines())


def translate_charts(app: BIApp, t: Translation) -> list[ChartSQL]:
    tr = ChartTranslator(app, t)
    notes = tr.model_notes()
    out = []
    for v in app.visuals:
        c = tr.visual(v)
        c.notes = notes + c.notes
        out.append(c)
    return out
