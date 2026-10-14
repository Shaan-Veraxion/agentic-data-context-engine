"""Finish a transpiled syntax tree as Dremio SQL.

Embedded SQL is parsed in its own dialect (Oracle, SQL Server, ...) and rendered with sqlglot's Dremio dialect.
That dialect leaves some constructs in a form Dremio does not accept (STR_TO_DATE with strftime formats, DATEDIFF
with three arguments, table hints, source-specific functions). This pass rewrites them on the tree into Dremio
functions, written literally, and rejects what has no safe equivalent.
"""
from __future__ import annotations

from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

from .expressions import Untranslatable
from .formats import dremio_call

_STRF_TO_DREMIO = [("%Y", "YYYY"), ("%y", "YY"), ("%m", "MM"), ("%-m", "MM"), ("%d", "DD"), ("%-d", "DD"),
                   ("%b", "MON"), ("%B", "MONTH"), ("%H", "HH24"), ("%-H", "HH24"), ("%M", "MI"), ("%S", "SS"),
                   ("%f", "FFF"), ("%p", "AMPM"), ("%a", "DY"), ("%A", "DAY")]


def strftime_to_dremio(fmt: str) -> str:
    out, i = "", 0
    while i < len(fmt):
        for s, d in _STRF_TO_DREMIO:
            if fmt.startswith(s, i):
                out, i = out + d, i + len(s)
                break
        else:
            if fmt[i] == "%":
                raise Untranslatable(f"date format directive {fmt[i:i + 2]!r}")
            out, i = out + fmt[i], i + 1
    return out


def _fmt(node: SqlExpr) -> exp.Literal | None:
    f = node.args.get("format")
    return exp.Literal.string(strftime_to_dremio(f.this)) if isinstance(f, exp.Literal) and f.is_string else None


def _rewrite(node: SqlExpr) -> SqlExpr:
    if isinstance(node, (exp.StrToDate, exp.TsOrDsToDate)) and (f := _fmt(node)) is not None:
        return dremio_call("TO_DATE", node.this, f)
    if isinstance(node, exp.StrToTime) and (f := _fmt(node)) is not None:
        return dremio_call("TO_TIMESTAMP", node.this, f)
    if isinstance(node, exp.TimeToStr) and (f := _fmt(node)) is not None:
        return dremio_call("TO_CHAR", node.this, f)
    if isinstance(node, exp.TimeStrToTime):
        return exp.Cast(this=node.this, to=exp.DataType.build("TIMESTAMP"))
    if isinstance(node, exp.DateDiff):
        u = node.args.get("unit")
        unit = (u.name if u is not None else "DAY").upper()
        # sqlglot: DateDiff(this=end, expression=start); Dremio: TIMESTAMPDIFF(unit, start, end)
        return dremio_call("TIMESTAMPDIFF", exp.var(unit), node.expression, node.this)
    if isinstance(node, exp.Table) and node.args.get("hints"):
        node.set("hints", None)  # WITH (NOLOCK) and similar: locking hints have no meaning in Dremio
        return node
    if isinstance(node, exp.Anonymous) and str(node.this).upper() == "RAWTOHEX":
        return exp.Upper(this=dremio_call("TO_HEX", *node.expressions))
    if isinstance(node, exp.Column) and not node.table and node.name.upper() == "ROWNUM":
        raise Untranslatable("Oracle ROWNUM (row limit or numbering) has no direct Dremio equivalent", "T2")
    return node


def finish(tree: SqlExpr) -> SqlExpr:
    # a rewrite can move nodes under a new parent that the same pass does not revisit: repeat to a fixed point
    for _ in range(4):
        before = tree.sql(dialect="dremio")
        tree = tree.transform(_rewrite)
        if tree.sql(dialect="dremio") == before:
            break
    return tree
