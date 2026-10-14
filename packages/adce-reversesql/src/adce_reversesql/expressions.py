"""ExprAST -> sqlglot expressions (Dremio SQL), with Qlik semantics made explicit.

Qlik semantics that differ from plain SQL and are reproduced here:
- '&' concatenation treats NULL as an empty string
- division by zero gives NULL (x / NULLIF(y, 0))
- relational and logical operators yield -1 (true) / 0 (false) when used as values
- a non-boolean value used as a condition is true when it is non-zero
- LIKE uses * and ? wildcards and is case-insensitive (ILIKE with % and _)
- Date(), Time(), Timestamp(), Num(), Money(), Dual() only change formatting, so the value passes through unchanged
- ApplyMap('Map', expr, default) is a left join to the mapping table (first match per key: MIN(value) per key,
  with key uniqueness checked at validation)
Functions without a safe translation are reported as untranslatable (tier T2/T3) and go to the AI route or review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from adce_core.bi_ir import Expr
from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr


class Untranslatable(ValueError):
    def __init__(self, what: str, tier: str = "T2") -> None:
        super().__init__(what)
        self.tier = tier


@dataclass
class MappingJoin:
    alias: str
    mapping: str  # mapping table name (Qlik)
    key: SqlExpr


@dataclass
class ExprContext:
    """What an expression can see: the input relation's alias and columns, and the mapping tables in scope."""

    alias: str
    columns: list[str] | None  # None when unknown (e.g. SELECT * from a source without a schema)
    mappings: dict[str, str]  # Qlik mapping table name -> CTE name
    joins: list[MappingJoin] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


BOOLEAN_OPS = {"=", "<>", "<", ">", "<=", ">=", "AND", "OR", "XOR", "NOT", "LIKE", "PRECEDES", "FOLLOWS"}
PASS_THROUGH = {"date", "time", "timestamp", "num", "money", "dual", "interval"}  # formatting only
NON_DETERMINISTIC = {"now", "today", "rand", "recno", "rowno"}

_SIMPLE = {  # Qlik function -> (SQL function, number of args accepted)
    "upper": ("UPPER", 1), "lower": ("LOWER", 1), "trim": ("TRIM", 1), "ltrim": ("LTRIM", 1),
    "rtrim": ("RTRIM", 1), "len": ("LENGTH", 1), "capitalize": ("INITCAP", 1), "replace": ("REPLACE", 3),
    "fabs": ("ABS", 1), "sqrt": ("SQRT", 1), "exp": ("EXP", 1), "log": ("LN", 1), "log10": ("LOG10", 1),
    "pow": ("POWER", 2), "mod": ("MOD", 2), "sign": ("SIGN", 1), "coalesce": ("COALESCE", None),
    "alt": ("COALESCE", None), "sum": ("SUM", 1), "min": ("MIN", 1), "max": ("MAX", 1), "avg": ("AVG", 1),
    "count": ("COUNT", 1), "year": ("YEAR", 1), "month": ("MONTH", 1), "day": ("DAY", 1),
}
_AGG = {"sum", "min", "max", "avg", "count", "concat", "only", "mode", "median"}


def is_boolean(e: Expr) -> bool:
    return (e.kind == "binop" and (e.name or "") in BOOLEAN_OPS) or (e.kind == "unop" and e.name == "NOT") \
        or (e.kind == "call" and (e.name or "").lower() in ("isnull", "isnum", "istext", "exists"))


def is_aggregate(e: Expr) -> bool:
    return (e.kind == "call" and (e.name or "").lower() in _AGG) or any(is_aggregate(a) for a in e.args)


class Translator:
    def __init__(self, ctx: ExprContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ entry points
    def value(self, e: Expr) -> SqlExpr:
        """Translate an expression used as a value (a field)."""
        if is_boolean(e):
            return exp.Case(ifs=[exp.If(this=self._bool(e), true=exp.Literal.number(-1))],
                            default=exp.Literal.number(0))
        return self._val(e)

    def condition(self, e: Expr) -> SqlExpr:
        """Translate an expression used as a condition (WHERE)."""
        return self._bool(e)

    # ------------------------------------------------------------------ booleans
    def _bool(self, e: Expr) -> SqlExpr:
        if e.kind == "binop" and e.name in ("AND", "OR"):
            a, b = self._bool(e.args[0]), self._bool(e.args[1])
            return exp.And(this=a, expression=b) if e.name == "AND" else exp.Or(this=a, expression=b)
        if e.kind == "binop" and e.name == "XOR":
            a, b = self._bool(e.args[0]), self._bool(e.args[1])
            return exp.NEQ(this=exp.Paren(this=a), expression=exp.Paren(this=b))
        if e.kind == "unop" and e.name == "NOT":
            return exp.Not(this=exp.Paren(this=self._bool(e.args[0])))
        if e.kind == "binop" and e.name in ("=", "<>", "<", ">", "<=", ">="):
            a, b = self._val(e.args[0]), self._val(e.args[1])
            cls = {"=": exp.EQ, "<>": exp.NEQ, "<": exp.LT, ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE}[e.name]
            return cls(this=a, expression=b)
        if e.kind == "binop" and e.name == "LIKE":
            pat = e.args[1]
            if pat.kind != "string":
                raise Untranslatable("LIKE with a non-literal pattern")
            sql_pat = str(pat.value).replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
            return exp.ILike(this=self._val(e.args[0]), expression=exp.Literal.string(sql_pat))
        if e.kind == "call" and (e.name or "").lower() == "isnull":
            return exp.Is(this=self._val(e.args[0]), expression=exp.Null())
        if e.kind == "binop" and e.name in ("PRECEDES", "FOLLOWS"):
            raise Untranslatable(f"{e.name} operator")
        return exp.NEQ(this=self._val(e), expression=exp.Literal.number(0))  # Qlik: non-zero is true

    # ------------------------------------------------------------------ values
    def _val(self, e: Expr) -> SqlExpr:
        k = e.kind
        if k == "number":
            return exp.Literal.number(e.value)
        if k == "string":
            return exp.Literal.string(str(e.value))
        if k == "field":
            name = e.name or ""
            if self.ctx.columns is not None and name not in self.ctx.columns:
                self.ctx.unknown_fields.append(name)
            return exp.column(name, table=self.ctx.alias, quoted=True)
        if k == "star":
            return exp.Star()
        if is_boolean(e):
            return self.value(e)
        if k == "unop":
            inner = self._val(e.args[0])
            return exp.Neg(this=inner) if e.name == "-" else inner
        if k == "binop":
            return self._binop(e)
        if k == "call":
            return self._call(e)
        raise Untranslatable(f"expression kind {k}")

    def _binop(self, e: Expr) -> SqlExpr:
        op = e.name or ""
        a, b = self._val(e.args[0]), self._val(e.args[1])
        if op == "&":
            def text(x: SqlExpr) -> SqlExpr:
                if isinstance(x, exp.DPipe) or (isinstance(x, exp.Literal) and x.is_string):
                    return x  # already non-null text
                return exp.Coalesce(this=exp.Cast(this=x, to=exp.DataType.build("VARCHAR")),
                                    expressions=[exp.Literal.string("")])
            return exp.DPipe(this=text(a), expression=text(b), safe=True)
        if op == "+":
            return exp.Add(this=a, expression=b)
        if op == "-":
            return exp.Sub(this=a, expression=b)
        if op == "*":
            return exp.Mul(this=a, expression=b)
        if op == "/":
            return exp.Div(this=a, expression=exp.Nullif(this=b, expression=exp.Literal.number(0)))
        raise Untranslatable(f"operator {op}")

    def _call(self, e: Expr) -> SqlExpr:
        name = (e.name or "").lower()
        args = e.args
        if name in NON_DETERMINISTIC:
            self.ctx.notes.append(f"{e.name}() is not deterministic; results depend on load order or time")
            if name == "today":
                return exp.CurrentDate()
            if name == "now":
                return exp.CurrentTimestamp()
            raise Untranslatable(f"{e.name}() depends on Qlik load order", "T2")
        if name in PASS_THROUGH and args:
            if name == "dual" and len(args) == 2:
                return self._val(args[1])  # the numeric part is the value used in calculations
            return self._val(args[0])
        if name == "if":
            cond = self._bool(args[0])
            then = self._val(args[1]) if len(args) > 1 else exp.Null()
            other = self._val(args[2]) if len(args) > 2 else exp.Null()
            return exp.Case(ifs=[exp.If(this=cond, true=then)], default=other)
        if name == "applymap":
            return self._applymap(e)
        if name in ("left", "right") and len(args) == 2:
            return exp.func(name.upper(), self._val(args[0]), self._val(args[1]), dialect="dremio")
        if name == "mid" and len(args) in (2, 3):
            parts = [self._val(a) for a in args]
            return exp.Substring(this=parts[0], start=parts[1], length=parts[2] if len(parts) == 3 else None)
        if name == "index" and len(args) == 2:
            return exp.StrPosition(this=self._val(args[0]), substr=self._val(args[1]))
        if name == "round" and len(args) == 1:
            return exp.Round(this=self._val(args[0]))
        if name in ("floor", "ceil") and len(args) == 1:
            cls = exp.Floor if name == "floor" else exp.Ceil
            return cls(this=self._val(args[0]))
        if name in ("daystart", "dayname") and len(args) == 1:
            return exp.Cast(this=self._val(args[0]), to=exp.DataType.build("DATE"))
        if name == "monthstart" and len(args) == 1:
            return exp.DateTrunc(this=self._val(args[0]), unit=exp.Literal.string("MONTH"))
        if name == "text" and len(args) == 1:
            return exp.Cast(this=self._val(args[0]), to=exp.DataType.build("VARCHAR"))
        if name == "num#" or name == "evaluate":
            raise Untranslatable(f"{e.name}() needs format-aware parsing")
        if name == "null" and not args:
            return exp.Null()
        if name == "count" and e.modifier == "DISTINCT" and len(args) == 1:
            return exp.Count(this=exp.Distinct(expressions=[self._val(args[0])]))
        if name == "count" and len(args) == 1 and args[0].kind == "star":
            return exp.Count(this=exp.Star())
        if name == "concat" and len(args) in (1, 2):
            sep = self._val(args[1]) if len(args) == 2 else exp.Literal.string("")
            # built through the Dremio parser so it renders as LISTAGG in Dremio and DuckDB
            return exp.func("LISTAGG", self._val(args[0]), sep, dialect="dremio")
        if name in _SIMPLE:
            fn, n = _SIMPLE[name]
            if n is not None and len(args) != n:
                raise Untranslatable(f"{e.name}() with {len(args)} arguments")
            if e.modifier and e.modifier != "DISTINCT":
                raise Untranslatable(f"{e.name}({e.modifier} ...)")
            return exp.func(fn, *[self._val(a) for a in args], dialect="dremio")
        raise Untranslatable(f"function {e.name}()", "T2")

    def _applymap(self, e: Expr) -> SqlExpr:
        args = e.args
        if len(args) < 2 or args[0].kind != "string":
            raise Untranslatable("ApplyMap with a non-literal map name")
        mapping = str(args[0].value)
        if mapping not in self.ctx.mappings:
            raise Untranslatable(f"ApplyMap: mapping table {mapping!r} is not loaded before this statement")
        alias = f"m{len(self.ctx.joins) + 1}"
        self.ctx.joins.append(MappingJoin(alias=alias, mapping=mapping, key=self._val(args[1])))
        matched = exp.Is(this=exp.column("map_key", table=alias, quoted=True), expression=exp.Null())
        mapped = exp.column("map_value", table=alias, quoted=True)
        # Qlik: no match returns the default if given, otherwise the key itself
        fallback = self._val(args[2]) if len(args) > 2 else self._val(args[1])
        return exp.Case(ifs=[exp.If(this=matched, true=fallback)], default=mapped)
