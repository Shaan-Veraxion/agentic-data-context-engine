"""Date and number format strings: Qlik -> Dremio, and Dremio -> DuckDB (for the local venue).

Dremio's TO_DATE / TO_TIMESTAMP / TO_CHAR take Dremio format strings (YYYY, MM, DD, MON, HH24, MI, SS, FFF, AMPM).
The translator emits those functions literally, because sqlglot's Dremio dialect does not render them as Dremio
SQL; the local venue rewrites them to DuckDB's strptime / strftime.
"""
from __future__ import annotations

import re

from sqlglot import exp
from sqlglot.expressions.core import Expr as SqlExpr

DREMIO_FUNCS = {"TO_DATE", "TO_TIMESTAMP", "TO_CHAR", "TIMESTAMPDIFF", "TIMESTAMPADD", "TO_HEX"}

# Qlik tokens, longest first. "mm" is minutes only after an hour token, otherwise months (as Qlik reads it).
_QLIK = [("YYYY", "YYYY"), ("yyyy", "YYYY"), ("YY", "YY"), ("yy", "YY"), ("MMMM", "MONTH"), ("MMM", "MON"),
         ("MM", "MM"), ("mm", "MM"), ("M", "MM"), ("DD", "DD"), ("dd", "DD"), ("D", "DD"), ("d", "DD"),
         ("WWWW", "DAY"), ("WWW", "DY"), ("hh", "HH24"), ("h", "HH24"), ("ss", "SS"), ("s", "SS"), ("fff", "FFF"),
         ("TT", "AMPM")]
_DREMIO_TO_STRF = [("YYYY", "%Y"), ("YY", "%y"), ("MONTH", "%B"), ("MON", "%b"), ("MM", "%m"), ("DD", "%d"),
                   ("DAY", "%A"), ("DY", "%a"), ("HH24", "%H"), ("MI", "%M"), ("SS", "%S"), ("FFF", "%g"),
                   ("AMPM", "%p")]


class FormatError(ValueError):
    pass


def qlik_to_dremio(fmt: str) -> str:
    out, i, after_hour = "", 0, False
    while i < len(fmt):
        if fmt[i] in "'\"":
            raise FormatError("quoted text in a format")
        if fmt.startswith("mm", i) and after_hour:
            out, i = out + "MI", i + 2
            continue
        for q, d in _QLIK:
            if fmt.startswith(q, i):
                out, i = out + d, i + len(q)
                after_hour = after_hour or d == "HH24"
                break
        else:
            ch = fmt[i]
            if ch.isalpha():
                raise FormatError(f"format token {ch!r}")
            out, i = out + ch, i + 1
    return out


def dremio_to_strftime(fmt: str) -> str:
    out, i = "", 0
    while i < len(fmt):
        for d, s in _DREMIO_TO_STRF:
            if fmt.startswith(d, i):
                out, i = out + s, i + len(d)
                break
        else:
            out, i = out + fmt[i].replace("%", "%%"), i + 1
    return out


def dremio_call(name: str, *args: SqlExpr) -> SqlExpr:
    """A Dremio function emitted as written (not re-rendered by a dialect)."""
    return exp.Anonymous(this=name, expressions=list(args))


_UNITS = {"YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND", "MILLISECOND"}


def to_duckdb(node: SqlExpr) -> SqlExpr:
    """Rewrite Dremio date functions in a tree parsed from Dremio SQL into DuckDB functions."""
    if isinstance(node, exp.TimestampDiff):
        # sqlglot's Dremio reader stores TIMESTAMPDIFF(unit, start, end) as this=unit, expression=start, unit=end
        if node.this is not None and node.this.name.upper() in _UNITS:
            unit, start, end = node.this.name, node.expression, node.args.get("unit")
        else:
            u = node.args.get("unit")
            unit, start, end = (u.name if u is not None else "DAY"), node.expression, node.this
        return exp.Anonymous(this="DATE_DIFF", expressions=[exp.Literal.string(unit.lower()), start, end])
    if isinstance(node, exp.ToChar) and isinstance(f := node.args.get("format"), exp.Literal):
        return exp.Anonymous(this="STRFTIME", expressions=[node.this, exp.Literal.string(dremio_to_strftime(f.this))])
    if not isinstance(node, exp.Anonymous) or str(node.this).upper() not in DREMIO_FUNCS:
        return node
    args = node.expressions
    if str(node.this).upper() == "TIMESTAMPDIFF" and len(args) == 3:
        return exp.Anonymous(this="DATE_DIFF", expressions=[exp.Literal.string(args[0].name.lower()), args[1], args[2]])
    if str(node.this).upper() == "TIMESTAMPADD" and len(args) == 3:  # (unit, n, date): DuckDB adds an interval
        unit = args[0].name.lower()
        months = exp.Anonymous(this=f"TO_{unit.upper()}S", expressions=[args[1]])
        return exp.Cast(this=exp.Add(this=args[2], expression=months), to=exp.DataType.build("DATE"))
    if str(node.this).upper() == "TO_HEX":
        return exp.Anonymous(this="HEX", expressions=list(args))
    if len(args) != 2 or not isinstance(args[1], exp.Literal):
        return node
    fmt = exp.Literal.string(dremio_to_strftime(args[1].this))
    name = str(node.this).upper()
    if name == "TO_CHAR":
        return exp.Anonymous(this="STRFTIME", expressions=[args[0], fmt])
    parsed = exp.Anonymous(this="STRPTIME", expressions=[exp.Cast(this=args[0], to=exp.DataType.build("VARCHAR")),
                                                         fmt])
    return exp.Cast(this=parsed, to=exp.DataType.build("DATE" if name == "TO_DATE" else "TIMESTAMP"))


def month_names(value: str | None) -> list[str]:
    names = [n for n in re.split(r";", value or "") if n]
    return names if len(names) == 12 else ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                           "Nov", "Dec"]
