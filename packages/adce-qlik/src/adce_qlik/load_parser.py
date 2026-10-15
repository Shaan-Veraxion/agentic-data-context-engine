"""Parse a LOAD statement into fields, source, filters and grouping, with expressions as ExprAST."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import cast

from adce_core.bi_ir import Expr, FieldDef, LoadSource
from lark import Lark, Token, Tree
from lark.exceptions import LarkError

GRAMMAR = Path(__file__).with_name("load.lark")
TABLE_IS = re.compile(r"""(?i)\btable\s+is\s+(\[[^\]]+\]|"[^"]+"|'[^']+'|[^,)]+)""")


def file_placeholder(ref: str, table: str | None) -> str:
    """Name of a file input: the path, and the sheet/table inside it when the format names one."""
    return f"{ref}#{table}" if table else ref


@lru_cache
def _parser() -> Lark:
    return Lark(GRAMMAR.read_text(encoding="utf-8"), parser="lalr", lexer="contextual", propagate_positions=True,
                maybe_placeholders=True)


@dataclass
class ParsedLoad:
    fields: list[FieldDef]
    source: LoadSource
    distinct: bool = False
    where: Expr | None = None
    while_: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)


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
            name, modifier, args = node.children
            mod = str(modifier.children[0]).upper() if isinstance(modifier, Tree) else None
            arg_nodes = args.children if isinstance(args, Tree) else []
            return Expr(kind="call", name=str(name), modifier=mod, args=[self._arg(a) for a in arg_nodes], text=text)
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
            m = TABLE_IS.search(fmt_text or "")
            return LoadSource(kind=kind, ref=path, format=fmt_text,
                              file_table=unquote_name(m.group(1).strip()) if m else None)
        if t == "resident":
            return LoadSource(kind="resident", ref=unquote_name(str(node.children[1].children[0])))
        if t == "inline":
            body, fmt = node.children[1], node.children[2]
            raw = unquote_name(str(body.children[0]))
            rows = [_inline_cells(line) for line in raw.strip().splitlines() if line.strip()]
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


def _inline_cells(line: str) -> list[str]:
    """One INLINE row: comma separated; a cell in double or single quotes may hold commas, and the quotes are
    removed (a doubled quote inside stands for one quote)."""
    cells, cur, quote, i = [], "", None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote and line[i + 1:i + 2] == quote:
                cur, i = cur + ch, i + 1
            elif ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'" and not cur.strip():
            cur, quote = "", ch
        elif ch == ",":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    cells.append(cur.strip())
    return cells


def parse_load(text: str) -> ParsedLoad:
    """text: the LOAD statement after label/prefix removal and variable expansion."""
    try:
        tree = _parser().parse(text)
    except LarkError as e:
        raise LoadParseError(str(e).splitlines()[0]) from e
    b = _Builder(text)
    _load, distinct, fields, source, filt, group, _order = cast(list[Tree | None], tree.children)
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
    return parsed
