"""ExprAST -> sqlglot expressions (Dremio SQL), with Qlik semantics made explicit.

Qlik semantics that differ from plain SQL and are reproduced here:
- '&' concatenation treats NULL as an empty string
- division by zero gives NULL (x / NULLIF(y, 0))
- relational and logical operators yield -1 (true) / 0 (false) when used as values
- a non-boolean value used as a condition is true when it is non-zero
- LIKE uses * and ? wildcards and is case-insensitive (ILIKE with % and _)
- Date(), Time(), Timestamp(), Num(), Money(), Dual() only change formatting, so the value passes through unchanged;
  inside '&' the text form is used: Date(x, fmt) is formatted (TO_CHAR) and Month(x) gives the month name from the
  script's MonthNames
- Date#() / Timestamp#() parse text with a Qlik format (TO_DATE / TO_TIMESTAMP with the Dremio format)
- ApplyMap('Map', expr, default) is a left join to the mapping table (first match per key: MIN(value) per key,
  with key uniqueness checked at validation)
Functions without a safe translation are reported as untranslatable (tier T2/T3) and go to the AI route or review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from adce_core.bi_ir import Expr
from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

from .formats import FormatError, dremio_call, month_names, qlik_to_dremio


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
    variables: dict[str, str] = field(default_factory=dict)  # script settings: MonthNames, DateFormat, ...
    row_column: str | None = None  # row-number column of a generated source (RecNo / RowNo)
    # Exists(): field name -> relations holding the values loaded so far (data tables, not mappings), and the
    # fields the current load itself adds (then Exists also sees the rows of this load: order-dependent)
    field_sources: dict[str, list[str]] = field(default_factory=dict)
    loading: set[str] = field(default_factory=set)


BOOLEAN_OPS = {"=", "<>", "<", ">", "<=", ">=", "AND", "OR", "XOR", "NOT", "LIKE", "PRECEDES", "FOLLOWS"}
PASS_THROUGH = {"date", "time", "timestamp", "num", "money", "dual", "interval"}  # formatting only
NON_DETERMINISTIC = {"now", "today", "rand", "recno", "rowno"}
FILE_FUNCTIONS = {"filetime", "filesize", "filename", "filepath", "filebasename", "filedir", "fileextension",
                  "qvdcreatetime", "qvdnoofrecords", "qvdnooffields", "qvdtablename", "qvdfieldname"}

_SIMPLE = {  # Qlik function -> (SQL function, number of args accepted)
    "upper": ("UPPER", 1), "lower": ("LOWER", 1), "trim": ("TRIM", 1), "ltrim": ("LTRIM", 1),
    "rtrim": ("RTRIM", 1), "len": ("LENGTH", 1), "capitalize": ("INITCAP", 1), "replace": ("REPLACE", 3),
    "fabs": ("ABS", 1), "sqrt": ("SQRT", 1), "exp": ("EXP", 1), "log": ("LN", 1), "log10": ("LOG10", 1),
    "pow": ("POWER", 2), "mod": ("MOD", 2), "sign": ("SIGN", 1), "coalesce": ("COALESCE", None),
    "alt": ("COALESCE", None), "sum": ("SUM", 1), "min": ("MIN", 1), "max": ("MAX", 1), "avg": ("AVG", 1),
    "count": ("COUNT", 1), "year": ("YEAR", 1), "month": ("MONTH", 1), "day": ("DAY", 1),
}
PERIOD_FUNCTIONS = {"quarterstart": "QUARTER", "quarterend": "QUARTER", "yearstart": "YEAR", "yearend": "YEAR",
                    "monthstart": "MONTH", "monthend": "MONTH"}
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
            inner = e.args[0]
            self_exists = inner.kind == "call" and (inner.name or "").lower() == "exists" and bool(inner.args)
            if self_exists and (inner.args[0].name or "") in self.ctx.loading:
                raise Untranslatable("NOT Exists() on a field this load also adds keeps the first row per value "
                                     "(depends on load order)", "T2")
            return exp.Not(this=exp.Paren(this=self._bool(inner)))
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
        if e.kind == "call" and (e.name or "").lower() == "exists":
            return self._exists(e)
        if e.kind == "call" and (e.name or "").lower() in ("isnum", "istext"):
            raise Untranslatable(f"{e.name}() (dual value types)", "T2")
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
            return exp.Neg(this=_paren(inner)) if e.name == "-" else inner
        if k == "binop":
            return self._binop(e)
        if k == "call":
            return self._call(e)
        raise Untranslatable(f"expression kind {k}")

    def _binop(self, e: Expr) -> SqlExpr:
        op = e.name or ""
        # sqlglot prints a tree built in code as is, without precedence parentheses: 2 * (1 + x) must keep them
        a, b = _paren(self._val(e.args[0])), _paren(self._val(e.args[1]))
        if op == "&":
            a, b = self._text(e.args[0], a), self._text(e.args[1], b)

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

    def _text(self, e: Expr, value: SqlExpr) -> SqlExpr:
        """The text form Qlik uses when a dual value is concatenated."""
        name = (e.name or "").lower() if e.kind == "call" else ""
        if name == "month" and len(e.args) == 1:
            names = month_names(self.ctx.variables.get("MonthNames"))
            return exp.Case(this=value, ifs=[exp.If(this=exp.Literal.number(i + 1), true=exp.Literal.string(n))
                                             for i, n in enumerate(names)])
        if name in ("date", "timestamp") and e.args:
            fmt_e = e.args[1] if len(e.args) > 1 else None
            if fmt_e is not None and fmt_e.kind != "string":
                raise Untranslatable(f"{e.name}() with a non-literal format")
            fmt = str(fmt_e.value) if fmt_e is not None else \
                self.ctx.variables.get("DateFormat" if name == "date" else "TimestampFormat")
            if fmt:
                try:
                    return dremio_call("TO_CHAR", self._val(e.args[0]), exp.Literal.string(qlik_to_dremio(fmt)))
                except FormatError as x:
                    raise Untranslatable(f"{e.name}() format {fmt!r}: {x}") from x
        return value

    def _exists(self, e: Expr) -> SqlExpr:
        """Exists(F [, expr]): the value (F's own, or expr) is among F's values loaded before this statement.
        In a WHERE, rows it rejects add no values, so the result does not depend on row order."""
        if not e.args or e.args[0].kind != "field" or len(e.args) > 2:
            raise Untranslatable("Exists() with a non-field first argument", "T2")
        f = e.args[0].name or ""
        probe = self._val(e.args[1]) if len(e.args) == 2 else self._val(e.args[0])
        rels = self.ctx.field_sources.get(f, [])
        if not rels:
            return exp.false()  # no table loaded the field yet: Exists is false
        q: SqlExpr = exp.select(exp.column(f, quoted=True)).from_(rels[0])
        for r in rels[1:]:
            q = exp.union(q, exp.select(exp.column(f, quoted=True)).from_(r), distinct=False)
        return exp.In(this=probe, query=exp.Subquery(this=q))

    def _round(self, args: list[Expr]) -> SqlExpr:
        """Qlik Round(x [, step [, offset]]) rounds half up to a multiple of step: Floor((x - o) / s + 0.5) * s + o.
        SQL ROUND rounds half away from zero, so Round(-2.5) would give -3 instead of Qlik's -2."""
        x = _paren(self._val(args[0]))
        step = _paren(self._val(args[1])) if len(args) > 1 else None
        off = _paren(self._val(args[2])) if len(args) > 2 else None
        v: SqlExpr = exp.Sub(this=x, expression=off) if off is not None else x
        if step is not None:
            v = exp.Div(this=exp.Paren(this=v), expression=step)
        v = exp.Floor(this=exp.Add(this=v, expression=exp.Literal.number(0.5)))
        if step is not None:
            v = exp.Mul(this=v, expression=step)
        return exp.Add(this=v, expression=off) if off is not None else v

    def _call(self, e: Expr) -> SqlExpr:
        name = (e.name or "").lower()
        args = e.args
        if name in ("recno", "rowno") and not args and self.ctx.row_column:
            return exp.column(self.ctx.row_column, table=self.ctx.alias, quoted=True)
        if name in FILE_FUNCTIONS:
            raise Untranslatable(f"{e.name}() reads file metadata at reload time", "T3")
        if name in ("date#", "timestamp#") and len(args) == 2:
            if args[1].kind != "string":
                raise Untranslatable(f"{e.name}() with a non-literal format")
            english = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
            script_months = [n.lower()[:3] for n in month_names(self.ctx.variables.get("MonthNames"))]
            if "MMM" in str(args[1].value) and script_months != english:
                # SQL date parsing knows English month names only; the script reads its own (MonthNames)
                raise Untranslatable(f"{e.name}() reads month names the script sets in MonthNames", "T2")
            try:
                fmt = qlik_to_dremio(str(args[1].value))
            except FormatError as x:
                raise Untranslatable(f"{e.name}() format: {x}") from x
            fn = "TO_DATE" if name == "date#" else "TO_TIMESTAMP"
            return dremio_call(fn, exp.Cast(this=self._val(args[0]), to=exp.DataType.build("VARCHAR")),
                               exp.Literal.string(fmt))
        if name == "yearname" and 1 <= len(args) <= 3:
            return self._yearname(args)
        if name in PERIOD_FUNCTIONS and len(args) == 1:
            return self._period(name, args[0])
        if name == "pick" and len(args) >= 2:
            sel = self._val(args[0])
            return exp.Case(this=sel, ifs=[exp.If(this=exp.Literal.number(i), true=self._val(a))
                                           for i, a in enumerate(args[1:], start=1)])
        if name in ("match", "mixmatch", "wildmatch") and len(args) >= 2:
            return self._match(name, args)
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
        if name in ("div", "mod") and len(args) == 2:
            x, y = _paren(self._val(args[0])), _paren(self._val(args[1]))
            q = exp.Div(this=x, expression=exp.Nullif(this=y, expression=exp.Literal.number(0)))
            if name == "div":  # integer part, truncated towards zero: Div(-7, 2) = -3
                return exp.Case(ifs=[exp.If(this=exp.GTE(this=q, expression=exp.Literal.number(0)),
                                            true=exp.Floor(this=q))], default=exp.Ceil(this=q))
            # Qlik's modulo is never negative for a positive divisor: Mod(-13, 3) = 2 (SQL MOD gives -1)
            return exp.Sub(this=x, expression=exp.Mul(this=y, expression=exp.Floor(this=q)))
        if name == "round" and 1 <= len(args) <= 3:
            return self._round(args)
        if name in ("floor", "ceil") and len(args) == 1:
            cls = exp.Floor if name == "floor" else exp.Ceil
            return cls(this=self._val(args[0]))
        if name in ("daystart", "dayname") and len(args) == 1:
            return exp.Cast(this=self._val(args[0]), to=exp.DataType.build("DATE"))
        if name == "text" and len(args) == 1:
            v = self._text(args[0], self._val(args[0]))  # the text Qlik shows: Date(x, fmt) keeps its format
            return v if _is_text(v) else exp.Cast(this=v, to=exp.DataType.build("VARCHAR"))
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

    def _period(self, name: str, arg: Expr) -> SqlExpr:
        """QuarterStart / YearStart / MonthStart and their ...End: calendar periods (FirstMonthOfYear = 1)."""
        unit = PERIOD_FUNCTIONS[name]
        if unit != "MONTH" and self.ctx.variables.get("FirstMonthOfYear", "1") not in ("1", ""):
            raise Untranslatable(f"{name}() with FirstMonthOfYear other than 1")
        start = exp.DateTrunc(this=self._val(arg), unit=exp.Literal.string(unit))
        if name.endswith("start"):
            return start
        months = {"QUARTER": 3, "YEAR": 12, "MONTH": 1}[unit]
        # Qlik returns the last millisecond of the period; its date part is the period's last day
        nxt = exp.Add(this=start, expression=exp.Interval(this=exp.Literal.string(str(months)), unit=exp.var("MONTH")))
        return exp.Sub(this=nxt, expression=exp.Interval(this=exp.Literal.string("1"), unit=exp.var("DAY")))

    def _match(self, name: str, args: list[Expr]) -> SqlExpr:
        """Match / MixMatch / WildMatch: position of the first matching value, 0 when none matches."""
        subject = self._val(args[0])
        ifs = []
        for i, a in enumerate(args[1:], start=1):
            if name == "wildmatch":
                if a.kind != "string":
                    raise Untranslatable("WildMatch with a non-literal pattern")
                pat = str(a.value).replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
                cond: SqlExpr = exp.ILike(this=subject.copy(), expression=exp.Literal.string(pat))
            elif name == "mixmatch":
                cond = exp.EQ(this=exp.Lower(this=subject.copy()), expression=exp.Lower(this=self._val(a)))
            else:
                cond = exp.EQ(this=subject.copy(), expression=self._val(a))
            ifs.append(exp.If(this=cond, true=exp.Literal.number(i)))
        return exp.Case(ifs=ifs, default=exp.Literal.number(0))

    def _yearname(self, args: list[Expr]) -> SqlExpr:
        """YearName(date [, shift [, first_month]]): 'YYYY', or 'YYYY-YYYY' for a fiscal year."""
        consts = []
        for a in args[1:]:
            if a.kind != "number":
                raise Untranslatable("YearName() with a non-constant shift or first month")
            consts.append(int(float(str(a.value))))
        shift = consts[0] if consts else 0
        first = consts[1] if len(consts) > 1 else 1
        d = self._val(args[0])

        def year(offset: int) -> SqlExpr:
            y: SqlExpr = exp.Year(this=d.copy())
            if offset:
                y = exp.Add(this=y, expression=exp.Literal.number(offset))
            return exp.Cast(this=y, to=exp.DataType.build("VARCHAR"))

        if first <= 1:
            return year(shift)

        def span(o: int) -> SqlExpr:
            return exp.DPipe(this=exp.DPipe(this=year(o), expression=exp.Literal.string("-")), expression=year(o + 1))

        in_next = exp.GTE(this=exp.Month(this=d.copy()), expression=exp.Literal.number(first))
        return exp.Case(ifs=[exp.If(this=in_next, true=span(shift))], default=span(shift - 1))

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


def _split_top(text: str) -> list[str]:
    """Split a Qlik argument list on top-level commas (not inside quotes or parentheses)."""
    out, depth, cur, q = [], 0, "", ""
    for ch in text:
        if q:
            cur += ch
            q = "" if ch == q else q
            continue
        if ch in "'\"[":
            q = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _paren(x: SqlExpr) -> SqlExpr:
    """Parenthesise an operand that is itself an operation, so the printed SQL keeps the Qlik grouping."""
    if isinstance(x, (exp.Binary, exp.Neg, exp.Not)) and not isinstance(x, exp.Dot):
        return exp.Paren(this=x)
    return x


def _is_text(x: SqlExpr) -> bool:
    return isinstance(x, exp.DPipe) or (isinstance(x, exp.Literal) and x.is_string) or (
        isinstance(x, exp.Anonymous) and x.name.upper() == "TO_CHAR")
