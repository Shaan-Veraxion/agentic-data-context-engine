"""Parse a LOAD statement into fields, source, filters and grouping, with expressions as ExprAST."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import cast

from adce_core.bi_ir import Expr, FieldDef, LoadSource
from lark import Lark, Token, Tree
from lark.exceptions import LarkError

GRAMMAR = Path(__file__).with_name("load.lark")


@lru_cache
def _parser() -> Lark:
    return Lark(GRAMMAR.read_text(encoding="utf-8"), parser="lalr", lexer="contextual", propagate_positions=True,
                maybe_placeholders=True, start=["start", "expr_only"])


@dataclass
class ParsedLoad:
    fields: list[FieldDef]
    source: LoadSource
    distinct: bool = False
    where: Expr | None = None
    while_: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    order_by: list[Expr] = field(default_factory=list)
    order_desc: list[bool] = field(default_factory=list)


class LoadParseError(ValueError):
    pass


def unquote_name(tok: str) -> str:
    if tok.startswith("[") and tok.endswith("]"):
        return tok[1:-1].replace("]]", "]")
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'`":
        q = tok[0]
        return tok[1:-1].replace(q + q, q)
    return tok


class _Builder:
    def __init__(self, text: str) -> None:
        self.text = text

    def slice(self, node: Tree | Token) -> str:
        if isinstance(node, Token):
            return str(node)
        meta = node.meta
        if getattr(meta, "empty", True):
            return ""
        return self.text[meta.start_pos:meta.end_pos]

    # ------------------------------------------------------------------ expressions
    def expr(self, node: Tree | Token) -> Expr:
        if isinstance(node, Token):  # bare token reaching here (e.g. inlined atom)
            return Expr(kind="field", name=unquote_name(str(node)), text=str(node))
        t = node.data
        text = self.slice(node)
        if t == "number":
            v = str(node.children[0])
            return Expr(kind="number", value=float(v) if any(c in v for c in ".eE") else int(v), text=text)
        if t == "string":
            return Expr(kind="string", value=unquote_name(str(node.children[0])), text=text)
        if t == "field_ref":
            return Expr(kind="field", name=unquote_name(str(node.children[0])), text=text)
        if t == "binop":
            left, op, right = node.children
            return Expr(kind="binop", name=str(op).upper(), args=[self.expr(left), self.expr(right)], text=text)
        if t == "unop":
            op, operand = node.children
            return Expr(kind="unop", name=str(op).upper(), args=[self.expr(operand)], text=text)
        if t == "call":
            name, set_tok, *mods, args = node.children
            words: list[str] = []
            total_dims: list[str] | None = None
            for m in mods:
                if isinstance(m, Tree):
                    words.append(str(m.children[0]).upper())
                    if len(m.children) > 1 and m.children[1] is not None:
                        total_dims = [unquote_name(x.strip()) for x in str(m.children[1])[1:-1].split(",") if x.strip()]
            arg_nodes = args.children if isinstance(args, Tree) else []
            return Expr(kind="call", name=str(name), modifier=" ".join(words) or None,
                        set_expr=str(set_tok) if set_tok is not None else None, total_dims=total_dims,
                        args=[self._arg(a) for a in arg_nodes], text=text)
        raise LoadParseError(f"unexpected expression node {t}")

    def _arg(self, node: Tree) -> Expr:
        if node.data == "star_arg":
            return Expr(kind="star", text="*")
        return self.expr(node.children[0])

    # ------------------------------------------------------------------ statement parts
    def fields(self, node: Tree) -> list[FieldDef]:
        out = []
        for f in node.children:
            if f.data == "star_field":
                out.append(FieldDef(name="*", expr=None, star=True))
                continue
            e_node, _as, alias = f.children
            e = self.expr(e_node)
            if alias is not None:
                name = unquote_name(str(alias.children[0]))
            elif e.kind == "field" and e.name:
                name = e.name
            else:
                name = e.text  # Qlik names an unaliased expression by its text
            out.append(FieldDef(name=name, expr=e))
        return out

    def source(self, node: Tree | None) -> LoadSource:
        if node is None:
            return LoadSource(kind="preceding")
        t = node.data
        if t == "from_file":
            _from, ref_node, fmt = node.children
            path = unquote_name(str(ref_node.children[0]))
            fmt_text = self._fmt(fmt)
            kind = "qvd" if path.lower().endswith(".qvd") or (fmt_text or "").strip().lower() == "qvd" else "file"
            return LoadSource(kind=kind, ref=path, format=fmt_text)
        if t == "resident":
            return LoadSource(kind="resident", ref=unquote_name(str(node.children[1].children[0])))
        if t == "inline":
            body, fmt = node.children[1], node.children[2]
            raw = unquote_name(str(body.children[0]))
            rows = _inline_rows(raw)
            return LoadSource(kind="inline", ref=None, format=self._fmt(fmt),
                              columns=rows[0] if rows else [], inline_rows=rows[1:])
        if t == "autogenerate":
            return LoadSource(kind="autogenerate", ref=self.slice(node.children[1]))
        if t == "extension":
            return LoadSource(kind="extension", ref=self.slice(node))
        raise LoadParseError(f"unexpected source {t}")

    @staticmethod
    def _fmt(fmt: Tree | None) -> str | None:
        if fmt is None:
            return None
        body = [str(c) for c in fmt.children if isinstance(c, Token) and c.type == "FMT_BODY"]
        return body[0].strip() if body else ""


def parse_load(text: str) -> ParsedLoad:
    """text: the LOAD statement after label/prefix removal and variable expansion."""
    try:
        tree = _parser().parse(text, start="start")
    except LarkError as e:
        raise LoadParseError(str(e).splitlines()[0]) from e
    b = _Builder(text)
    _load, distinct, fields, source, filt, group, order = cast(list[Tree | None], tree.children)
    parsed = ParsedLoad(fields=b.fields(cast(Tree, fields)), source=b.source(source),
                        distinct=distinct is not None)
    if filt is not None:
        e = b.expr(filt.children[1])
        if filt.data == "where":
            parsed.where = e
        else:
            parsed.while_ = e
    if group is not None:
        parsed.group_by = [b.expr(c) for c in group.children[2:]]
    if order is not None:
        for item in order.children:
            if isinstance(item, Tree) and item.data == "order_item":
                parsed.order_by.append(b.expr(item.children[0]))
                d = item.children[1] if len(item.children) > 1 else None
                parsed.order_desc.append(d is not None and str(d).lower() == "desc")
    return parsed


def parse_expression(text: str) -> Expr:
    """A chart expression (measure or calculated dimension); a leading '=' is allowed. Variables must already be
    expanded."""
    t = text.strip()
    t = t[1:].lstrip() if t.startswith("=") else t
    try:
        tree = _parser().parse(t, start="expr_only")
    except LarkError as e:
        raise LoadParseError(str(e).splitlines()[0]) from e
    return _Builder(t).expr(cast(Tree, tree).children[0])


def _inline_rows(raw: str) -> list[list[str]]:
    """INLINE data as Qlik reads it: comma-separated, cells may be quoted ("a, b" keeps its comma; the quotes, double
    or single, and brackets around a header name are removed)."""
    import csv

    lines = [line for line in raw.strip().splitlines() if line.strip()]
    out = []
    for cells in csv.reader(lines, skipinitialspace=True):
        row = []
        for c in cells:
            c = c.strip()
            if len(c) >= 2 and (c[0], c[-1]) in (("'", "'"), ('"', '"'), ("[", "]")):
                c = c[1:-1]
            row.append(c)
        out.append(row)
    return out
