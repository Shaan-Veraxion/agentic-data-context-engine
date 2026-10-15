"""Compose Dremio SQL from the BI IR: one CTE per Qlik load step, in script execution order.

Each Qlik table is a sequence of versions. A step (load chain + its action) produces a new version:
- CREATE: the chain's output
- CONCATENATE / AUTO_CONCATENATE: UNION ALL of the current version and the chain, columns aligned (missing = NULL)
- JOIN: Qlik joins on every field the two sides share (no shared field = cross join); outer/right joins merge keys
- KEEP: semi-join filters (left keep reduces the loaded table, right keep the other table, inner keep both)
- MAPPING: kept as a two-column relation for ApplyMap
RESIDENT reads the version current at that statement; DROP ends a table; STORE snapshots the version stored.
Sources keep the names Qlik used: embedded SQL keeps its Dremio paths, or is converted from its own dialect and
mapped to the Dremio source of its connection (sources.py); QVD and file inputs are placeholders
("__qvd__"."<path>", "__file__"."<path>", or "__file__"."<path>#<sheet>" when the format names a sheet or
table inside the file) resolved through QVD chains or the catalog (phase PD), and mapped to
local data in the local venue.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from adce_core.bi_ir import BIApp, LoadOp, QlikTable, TableAction, TableStep
from adce_qlik.load_parser import file_placeholder
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.expressions.core import Expr as SqlExpr

from .expressions import ExprContext, Translator, Untranslatable, is_aggregate
from .sources import Connection, to_dremio

DIALECT = "dremio"
QVD_DB = "__qvd__"
FILE_DB = "__file__"


@dataclass
class Cte:
    name: str
    body: SqlExpr
    columns: list[str] | None
    statements: list[int]
    spans: list[str]
    kind: str  # source | load | join | concat | keep | mapping
    deps: list[str] = field(default_factory=list)
    ai: bool = False  # body came from the AI route (needs review)


@dataclass
class Untranslated:
    table: str
    statement: int
    span: str
    reason: str
    tier: str
    text: str
    input_columns: list[str] | None = None


@dataclass
class StepResult:
    action: str
    statements: list[int]
    spans: list[str]
    source_cte: str | None  # relation read by the bottom statement (SQL result, resident version, QVD, ...)
    chain_cte: str | None  # output of the load chain (rows added by this step)
    result_cte: str | None  # table version after the step
    start_line: int
    end_line: int


@dataclass
class TableResult:
    table: str
    mapping: bool
    final_cte: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    stores: list[tuple[str, str]] = field(default_factory=list)  # (QVD target, cte at store time)
    untranslated: list[Untranslated] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)  # placeholder inputs still to resolve (QVD, files)
    unresolved: list[str] = field(default_factory=list)  # $(variables) left in the SQL, here or in tables it reads
    files: list[str] = field(default_factory=list)  # placeholder file inputs, here or in tables it reads

    @property
    def complete(self) -> bool:
        return self.final_cte is not None and not self.untranslated and not self.unresolved and not self.files


@dataclass
class Translation:
    ctes: dict[str, Cte] = field(default_factory=dict)
    tables: dict[str, TableResult] = field(default_factory=dict)

    def render(self, target: str, dialect: str = DIALECT, comments: bool = True) -> str:
        order: list[str] = []

        def visit(n: str) -> None:
            if n in order:
                return
            for d in self.ctes[n].deps:
                visit(d)
            order.append(n)

        visit(target)
        parts = []
        for n in order:
            c = self.ctes[n]
            body = c.body.sql(dialect=dialect, pretty=True)
            note = ""
            if comments:
                where = ", ".join(c.spans) if c.spans else ""
                tag = " · AI-assisted, needs review" if c.ai else ""
                note = f"  -- {c.kind}: statements {_ranges(c.statements)} ({where}){tag}\n" if c.statements else ""
            parts.append(f"{n} AS (\n{note}{_indent(body)}\n)")
        return "WITH " + ",\n".join(parts) + f"\nSELECT * FROM {target}"

    def table_sql(self, table: str, dialect: str = DIALECT) -> str | None:
        t = self.tables.get(table)
        return self.render(t.final_cte, dialect) if t and t.final_cte else None


def _indent(s: str) -> str:
    return "\n".join("  " + ln for ln in s.splitlines())


def _ranges(nums: list[int]) -> str:
    nums = sorted(set(nums))
    out, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i == len(nums) - 1 or nums[i + 1] != n + 1:
            out.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(out)


def _ident(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower() or "t"
    return s if s[0].isalpha() else f"t_{s}"


def _num(v: str) -> int | float | None:
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


Override = Callable[[LoadOp, list[str] | None], str | None]


class Composer:
    def __init__(self, app: BIApp, overrides: dict[int, str] | None = None,
                 connections: dict[str, Connection] | None = None) -> None:
        self.app = app
        self.connections = connections or {}  # Qlik connection -> its database (dialect, Dremio path)
        self.overrides = overrides or {}  # executed statement index -> SQL body reading from "src" (AI route)
        self.t = Translation()
        self.cur: dict[str, tuple[str, list[str] | None]] = {}  # Qlik table -> (cte, columns)
        self.broken: set[str] = set()
        self.mappings: dict[str, str] = {}
        self.names: dict[str, int] = {}
        self.alias_of: dict[str, str] = {}  # old table name -> current name (RENAME TABLE)
        self._cur_op: LoadOp | None = None  # statement being translated (for precise untranslated reports)
        self._in_cols: list[str] | None = None
        self._read_unresolved: list[str] = []  # unresolved variables of the tables read by the current step
        self._read_files: list[str] = []  # placeholder file inputs of the tables read by the current step

    # ------------------------------------------------------------------ helpers
    def _name(self, table: str, suffix: str = "") -> str:
        base = f"t_{_ident(table)}{suffix}"
        self.names[base] = self.names.get(base, 0) + 1
        return f"{base}_{self.names[base]}"

    def _add(self, cte: Cte) -> str:
        cte.deps = [d for d in dict.fromkeys(
            t.name for t in cte.body.find_all(exp.Table) if not t.db and t.name in self.t.ctes)]
        self.t.ctes[cte.name] = cte
        return cte.name

    @staticmethod
    def _span(op: LoadOp) -> str:
        return f"{op.span.file}:{op.span.start_line}-{op.span.end_line}"

    def _result(self, table: str) -> TableResult:
        if table not in self.t.tables:
            qt = self.app.tables.get(table)
            self.t.tables[table] = TableResult(table=table, mapping=bool(qt and qt.mapping))
        return self.t.tables[table]

    def _resolve_name(self, name: str) -> str:
        return self.alias_of.get(name, name)

    # ------------------------------------------------------------------ main
    def compose(self) -> Translation:
        events: list[tuple[int, int, str, object]] = []
        for t in self.app.tables.values():
            for old in t.renamed_from:
                self.alias_of[old] = t.name
            for s in t.steps:
                events.append((s.ops[0].statement, 1, t.name, s))
            if t.dropped_at is not None:
                events.append((t.dropped_at, 0, t.name, "drop"))
        for st in self.app.stores:
            events.append((st.statement, 0, self._resolve_name(st.table), ("store", st.target)))
        events.sort(key=lambda e: (e[0], e[1]))
        for _, _, table, ev in events:
            if ev == "drop":
                self.cur.pop(table, None)
            elif isinstance(ev, tuple):
                if table in self.cur:
                    self._result(table).stores.append((ev[1], self.cur[table][0]))
            else:
                assert isinstance(ev, TableStep)
                self._step(self.app.tables[table], ev)
        for name, (cte, _) in self.cur.items():
            r = self._result(name)
            if name not in self.broken:
                r.final_cte = cte
        return self.t

    def _step(self, table: QlikTable, step: TableStep) -> None:
        r = self._result(table.name)
        spans = [self._span(o) for o in step.ops]
        stmts = [o.statement for o in step.ops]
        lines = (min(o.span.start_line for o in step.ops), max(o.span.end_line for o in step.ops))
        target = table.name
        if table.name in self.broken and step.action not in (TableAction.CREATE, TableAction.MAPPING):
            r.untranslated.append(Untranslated(target, stmts[0], spans[0], "depends on an untranslated step", "T1",
                                               step.ops[0].text))
            return
        self._read_unresolved, self._read_files = [], []
        try:
            source_cte, chain_cte, chain_cols = self._chain(target, step.ops)
            if step.qualified:
                chain_cte, chain_cols = self._qualify(target, step, chain_cte, chain_cols, stmts, spans)
        except Untranslatable as e:
            self.broken.add(target)
            if step.action == TableAction.KEEP and step.other_table:
                self.broken.add(step.other_table)
            self.cur.pop(target, None)
            return self._note_untranslated(r, step, str(e), e.tier)

        own = [v for o in step.ops for v in re.findall(r"\$\(([^)]*)\)", o.text)]
        r.unresolved = list(dict.fromkeys(r.unresolved + own + self._read_unresolved))
        r.files = list(dict.fromkeys(r.files + self._read_files))
        result = chain_cte
        if step.action == TableAction.MAPPING:
            self.mappings[target] = chain_cte
            self.cur[target] = (chain_cte, chain_cols)
        elif step.action == TableAction.CREATE:
            self.cur[target] = (chain_cte, chain_cols)
            self.broken.discard(target)
        elif step.action in (TableAction.CONCATENATE, TableAction.AUTO_CONCATENATE):
            result = self._concat(target, chain_cte, chain_cols, stmts, spans)
        elif step.action == TableAction.JOIN:
            result = self._join(target, chain_cte, chain_cols, step.join_kind or "outer", stmts, spans)
        elif step.action == TableAction.KEEP:
            result = self._keep(target, step, chain_cte, chain_cols, stmts, spans)
        r.steps.append(StepResult(action=step.action.value, statements=stmts, spans=spans, source_cte=source_cte,
                                  chain_cte=chain_cte, result_cte=result, start_line=lines[0], end_line=lines[1]))

    def _note_untranslated(self, r: TableResult, step: TableStep, reason: str, tier: str) -> None:
        op = self._cur_op if self._cur_op is not None and self._cur_op in step.ops else step.ops[-1]
        r.untranslated.append(Untranslated(r.table, op.statement, self._span(op), reason, tier, op.text,
                                           self._in_cols))

    # ------------------------------------------------------------------ load chains
    def _chain(self, table: str, ops: list[LoadOp]) -> tuple[str, str, list[str] | None]:
        bottom = ops[-1]
        self._cur_op, self._in_cols = bottom, None
        if bottom.parse_error:
            raise Untranslatable(f"statement does not parse: {bottom.parse_error}", "T1")
        src_cte, cols = self._source(table, bottom)
        cur_cte, cur_cols = src_cte, cols
        for op in reversed(ops if bottom.kind == "load" else ops[:-1]):
            cur_cte, cur_cols = self._load(table, op, cur_cte, cur_cols)
        return src_cte, cur_cte, cur_cols

    def _source(self, table: str, op: LoadOp) -> tuple[str, list[str] | None]:
        s = op.source
        span = [self._span(op)]
        if s.kind == "sql":
            try:
                body = sqlglot.parse_one(s.ref or "", read=s.dialect or DIALECT)
            except ParseError as e:
                raise Untranslatable(f"embedded SQL does not parse as {s.dialect or DIALECT} SQL: "
                                     f"{str(e).splitlines()[0]}", "T1") from e
            body = to_dremio(body, s.dialect, self.connections.get(s.connection or ""))
            cols = list(s.columns) or None
            return self._add(Cte(self._name(table, "_sql"), body, cols, [op.statement], span, "source")), cols
        if s.kind == "resident":
            ref = self._resolve_name(s.ref or "")
            if ref in self.broken:
                raise Untranslatable(f"RESIDENT {ref} depends on an untranslated step", "T1")
            if ref not in self.cur:
                raise Untranslatable(f"RESIDENT {ref}: table not loaded (or already dropped) at this point", "T1")
            self._read_unresolved += self._result(ref).unresolved
            self._read_files += self._result(ref).files
            return self.cur[ref]
        if s.kind == "inline":
            cols = list(s.columns)
            if not cols:
                raise Untranslatable("INLINE table without a header row", "T1")
            rows = [r + [""] * (len(cols) - len(r)) for r in s.inline_rows]
            numeric = [all(_num(r[i]) is not None for r in rows) and bool(rows) for i in range(len(cols))]
            if not rows:
                body: SqlExpr = exp.select(*[exp.alias_(exp.Null(), c, quoted=True) for c in cols]).where(
                    exp.false())
            else:
                tuples = [exp.Tuple(expressions=[exp.Literal.number(r[i]) if numeric[i] else exp.Literal.string(r[i])
                                                 for i in range(len(cols))]) for r in rows]
                values = exp.Values(expressions=tuples, alias=exp.TableAlias(
                    this=exp.to_identifier("v"), columns=[exp.to_identifier(c, quoted=True) for c in cols]))
                body = exp.select("*").from_(values)
            return self._add(Cte(self._name(table, "_inline"), body, cols, [op.statement], span, "source")), cols
        if s.kind in ("qvd", "file"):
            db = QVD_DB if s.kind == "qvd" else FILE_DB
            ref = file_placeholder(s.ref or "", s.file_table)
            tbl = exp.Table(this=exp.to_identifier(ref, quoted=True), db=exp.to_identifier(db, quoted=True))
            r = self._result(table)
            if ref not in r.inputs:
                r.inputs.append(ref)
            if s.kind == "file" and ref not in r.files:
                r.files.append(ref)
            cols = list(s.columns) or None
            body = exp.select(*([exp.column(c, quoted=True) for c in cols] if cols else ["*"])).from_(tbl)
            return self._add(Cte(self._name(table, f"_{s.kind}"), body, cols, [op.statement], span, "source")), cols
        if s.kind == "autogenerate":
            raise Untranslatable("AUTOGENERATE (row generator)", "T2")
        raise Untranslatable(f"source kind {s.kind}", "T2")

    def _load(self, table: str, op: LoadOp, in_cte: str, in_cols: list[str] | None) -> tuple[str, list[str] | None]:
        self._cur_op, self._in_cols = op, in_cols
        if op.statement in self.overrides:
            return self._override(table, op, in_cte, in_cols)
        if op.while_ is not None:
            raise Untranslatable("WHILE clause (row iteration)", "T3")
        ctx = ExprContext(alias="src", columns=in_cols, mappings=self.mappings)
        tr = Translator(ctx)
        proj: list[SqlExpr] = []
        out_cols: list[str] | None = []
        for f in op.fields:
            if f.star:
                if in_cols:
                    proj += [exp.alias_(exp.column(c, table="src", quoted=True), c, quoted=True) for c in in_cols]
                    out_cols = (out_cols or []) + [c for c in in_cols if c not in (out_cols or [])]
                else:
                    proj.append(exp.Column(this=exp.Star(), table=exp.to_identifier("src")))
                    out_cols = None
            else:
                assert f.expr is not None
                proj.append(exp.alias_(tr.value(f.expr), f.name, quoted=True))
                if out_cols is not None:
                    out_cols.append(f.name)
        src = exp.Table(this=exp.to_identifier(in_cte), alias=exp.TableAlias(this=exp.to_identifier("src")))
        sel = exp.select(*proj).from_(src)
        if op.distinct:
            sel = sel.distinct()
        if op.where is not None:
            sel = sel.where(tr.condition(op.where))
        if op.group_by:
            sel = sel.group_by(*[tr.value(g) for g in op.group_by])
        elif any(f.expr is not None and is_aggregate(f.expr) for f in op.fields):
            pass  # an aggregate over the whole input: a single row, valid without GROUP BY
        for j in ctx.joins:
            mcte = self.mappings[j.mapping]
            mcols = self.t.ctes[mcte].columns or []
            if len(mcols) < 2:
                raise Untranslatable(f"mapping table {j.mapping} needs two columns", "T1")
            sub = exp.select(exp.alias_(exp.column(mcols[0], quoted=True), "map_key", quoted=True),
                             exp.alias_(exp.Min(this=exp.column(mcols[1], quoted=True)), "map_value", quoted=True)
                             ).from_(mcte).group_by(exp.column(mcols[0], quoted=True))
            on = exp.EQ(this=exp.column("map_key", table=j.alias, quoted=True), expression=j.key)
            sel = sel.join(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(j.alias))),
                           on=on, join_type="left")
        r = self._result(table)
        if ctx.unknown_fields:
            r.unknown_fields += [f for f in ctx.unknown_fields if f not in r.unknown_fields]
        r.notes += [n for n in ctx.notes if n not in r.notes]
        if ctx.joins:
            r.notes.append("ApplyMap: first match per key assumed (MIN per key); key uniqueness is checked locally")
        name = self._add(Cte(self._name(table), sel, out_cols, [op.statement], [self._span(op)], "load"))
        return name, out_cols

    def _override(self, table: str, op: LoadOp, in_cte: str, in_cols: list[str] | None
                  ) -> tuple[str, list[str] | None]:
        try:
            body = sqlglot.parse_one(self.overrides[op.statement], read=DIALECT)
        except ParseError as e:
            raise Untranslatable(f"AI translation does not parse: {str(e).splitlines()[0]}", "T2") from e
        for t in body.find_all(exp.Table):
            if t.name.lower() == "src" and not t.db:
                t.set("this", exp.to_identifier(in_cte))
                t.set("alias", exp.TableAlias(this=exp.to_identifier("src")))
        cols = [e.alias_or_name for e in body.expressions] if isinstance(body, exp.Select) else None
        name = self._add(Cte(self._name(table), body, cols, [op.statement], [self._span(op)], "load", ai=True))
        return name, cols

    # ------------------------------------------------------------------ table actions
    def _qualify(self, table: str, step: TableStep, in_cte: str, in_cols: list[str] | None, stmts: list[int],
                 spans: list[str]) -> tuple[str, list[str]]:
        """QUALIFY: the loaded fields are named "Table.field"."""
        if in_cols is None:
            raise Untranslatable("QUALIFY over a load with unknown columns", "T1")
        cols = [step.qualified.get(c, c) for c in in_cols]
        body = exp.select(*[exp.alias_(exp.column(c, quoted=True), q, quoted=True)
                            for c, q in zip(in_cols, cols, strict=True)]).from_(in_cte)
        return self._add(Cte(self._name(table, "_qualify"), body, cols, stmts, spans, "load")), cols

    def _concat(self, table: str, new_cte: str, new_cols: list[str] | None, stmts: list[int], spans: list[str]) -> str:
        cur_cte, cur_cols = self.cur[table]
        if cur_cols is None or new_cols is None:
            raise Untranslatable("CONCATENATE with unknown columns on one side", "T1")
        cols = cur_cols + [c for c in new_cols if c not in cur_cols]

        def side(cte: str, have: list[str]) -> exp.Select:
            return exp.select(*[exp.alias_(exp.column(c, quoted=True) if c in have else exp.Null(), c, quoted=True)
                                for c in cols]).from_(cte)

        body = exp.union(side(cur_cte, cur_cols), side(new_cte, new_cols), distinct=False)
        name = self._add(Cte(self._name(table, "_concat"), body, cols, stmts, spans, "concat"))
        self.cur[table] = (name, cols)
        return name

    def _join(self, table: str, new_cte: str, new_cols: list[str] | None, kind: str, stmts: list[int],
              spans: list[str]) -> str:
        cur_cte, cur_cols = self.cur[table]
        if cur_cols is None or new_cols is None:
            raise Untranslatable("JOIN with unknown columns on one side (keys cannot be determined)", "T1")
        keys = [c for c in cur_cols if c in new_cols]
        proj: list[SqlExpr] = []
        for c in cur_cols:
            col = exp.column(c, table="t", quoted=True)
            if c in keys and kind in ("outer", "right"):
                col = exp.Coalesce(this=col, expressions=[exp.column(c, table="n", quoted=True)])
            proj.append(exp.alias_(col, c, quoted=True))
        proj += [exp.alias_(exp.column(c, table="n", quoted=True), c, quoted=True) for c in new_cols if c not in keys]
        t = exp.Table(this=exp.to_identifier(cur_cte), alias=exp.TableAlias(this=exp.to_identifier("t")))
        n = exp.Table(this=exp.to_identifier(new_cte), alias=exp.TableAlias(this=exp.to_identifier("n")))
        sel = exp.select(*proj).from_(t)
        if keys:
            on = exp.and_(*[exp.EQ(this=exp.column(k, table="t", quoted=True),
                                   expression=exp.column(k, table="n", quoted=True)) for k in keys])
            sel = sel.join(n, on=on, join_type={"outer": "full", "left": "left", "right": "right",
                                                "inner": "inner"}[kind])
        else:
            sel = sel.join(n, join_type="cross")
            self._result(table).notes.append("JOIN without shared fields is a cross join in Qlik")
        cols = cur_cols + [c for c in new_cols if c not in keys]
        name = self._add(Cte(self._name(table, "_join"), sel, cols, stmts, spans, "join"))
        self.cur[table] = (name, cols)
        return name

    def _keep(self, table: str, step: TableStep, new_cte: str, new_cols: list[str] | None, stmts: list[int],
              spans: list[str]) -> str:
        other = self._resolve_name(step.other_table or "")
        if other not in self.cur:
            raise Untranslatable(f"KEEP against {other!r}, which is not loaded at this point", "T1")
        o_cte, o_cols = self.cur[other]
        if o_cols is None or new_cols is None:
            raise Untranslatable("KEEP with unknown columns on one side", "T1")
        keys = [c for c in new_cols if c in o_cols]
        kind = step.join_kind or "inner"

        def reduce(x_cte: str, by_cte: str, label: str) -> str:
            cond = exp.and_(*[exp.EQ(this=exp.column(k, table="y", quoted=True),
                                     expression=exp.column(k, table="x", quoted=True)) for k in keys])
            inner = exp.select(exp.Literal.number(1)).from_(
                exp.Table(this=exp.to_identifier(by_cte), alias=exp.TableAlias(this=exp.to_identifier("y")))
            ).where(cond)
            body = exp.select(exp.Column(this=exp.Star(), table=exp.to_identifier("x"))).from_(
                exp.Table(this=exp.to_identifier(x_cte), alias=exp.TableAlias(this=exp.to_identifier("x")))
            ).where(exp.Exists(this=inner))
            return self._add(Cte(self._name(label, "_keep"), body, None, stmts, spans, "keep"))

        if not keys:
            raise Untranslatable("KEEP without shared fields", "T2")
        loaded = reduce(new_cte, o_cte, table) if kind in ("left", "inner") else new_cte
        self.t.ctes[loaded].columns = new_cols
        if kind in ("right", "inner"):
            reduced_other = reduce(o_cte, new_cte, other)
            self.t.ctes[reduced_other].columns = o_cols
            self.cur[other] = (reduced_other, o_cols)
        self.cur[table] = (loaded, new_cols)
        return loaded


def compose(app: BIApp, overrides: dict[int, str] | None = None,
            connections: dict[str, Connection] | None = None) -> Translation:
    return Composer(app, overrides, connections).compose()
