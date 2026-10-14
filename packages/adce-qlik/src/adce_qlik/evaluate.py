"""Evaluate literal Qlik expressions (conditions, loop bounds, LET values) after $(…) expansion.

Only constants are evaluated: numbers, strings, arithmetic, '&', comparisons, AND/OR/NOT/XOR and a few pure
functions. Anything that refers to fields or data (or an unknown function) is not evaluable: callers then keep
both possibilities and flag them instead of guessing.
"""
from __future__ import annotations

from adce_core.bi_ir import Expr

from .load_parser import LoadParseError, parse_load


class NotEvaluable(ValueError):
    pass


def parse_expr(text: str) -> Expr:
    try:
        fields = parse_load(f"LOAD {text} AS __v AUTOGENERATE 1").fields
    except LoadParseError as e:
        raise NotEvaluable(str(e)) from e
    if len(fields) != 1 or fields[0].expr is None:
        raise NotEvaluable(text)
    return fields[0].expr


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return -1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def truthy(v: object) -> bool:
    n = _num(v)
    return n != 0 if n is not None else bool(v)


def _cmp(op: str, a: object, b: object) -> bool:
    na, nb = _num(a), _num(b)
    x, y = (na, nb) if na is not None and nb is not None else (str(a), str(b))
    return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]  # type: ignore[operator]


_FUNCS = {
    "len": lambda a: len(str(a[0])), "upper": lambda a: str(a[0]).upper(), "lower": lambda a: str(a[0]).lower(),
    "trim": lambda a: str(a[0]).strip(), "left": lambda a: str(a[0])[: int(_num(a[1]) or 0)],
    "right": lambda a: str(a[0])[-int(_num(a[1]) or 0):] if int(_num(a[1]) or 0) else "",
    "if": lambda a: a[1] if truthy(a[0]) else (a[2] if len(a) > 2 else None),
}


def evaluate(e: Expr) -> object:
    k = e.kind
    if k in ("number", "string"):
        return e.value
    if k == "unop":
        v = evaluate(e.args[0])
        if e.name == "NOT":
            return -1 if not truthy(v) else 0
        n = _num(v)
        if n is None:
            raise NotEvaluable(e.text)
        return -n if e.name == "-" else n
    if k == "binop":
        op = e.name or ""
        a, b = evaluate(e.args[0]), evaluate(e.args[1])
        if op in ("AND", "OR", "XOR"):
            ta, tb = truthy(a), truthy(b)
            res = ta and tb if op == "AND" else ta or tb if op == "OR" else ta != tb
            return -1 if res else 0
        if op in ("=", "<>", "<", ">", "<=", ">="):
            return -1 if _cmp(op, a, b) else 0
        if op == "&":
            return f"{'' if a is None else a}{'' if b is None else b}"
        na, nb = _num(a), _num(b)
        if na is None or nb is None:
            raise NotEvaluable(e.text)
        if op == "+":
            return na + nb
        if op == "-":
            return na - nb
        if op == "*":
            return na * nb
        if op == "/":
            return None if nb == 0 else na / nb
        raise NotEvaluable(e.text)
    if k == "call" and (e.name or "").lower() in _FUNCS:
        return _FUNCS[(e.name or "").lower()]([evaluate(a) for a in e.args])
    raise NotEvaluable(e.text)


def evaluate_text(text: str) -> object:
    return evaluate(parse_expr(text))


def as_text(v: object) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "" if v is None else str(v)
