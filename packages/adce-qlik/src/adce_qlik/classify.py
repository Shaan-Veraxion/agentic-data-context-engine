"""Classify split statements: label, load prefixes and statement kind."""
from __future__ import annotations

import re

from adce_core.bi_ir import Statement, StatementKind as K

from .splitter import RawStatement

LABEL = re.compile(r"""^\s*(?P<label>\[[^\]]*\]|"[^"]*"|\$\([^)]*\)|[^\s:;\[\]"'(),=$]+)\s*:(?![:/=\\])""")

_P = r"\s*(?:\([^)]*\))?"  # optional "(TableName)"
PREFIXES = [re.compile(p, re.IGNORECASE) for p in (
    r"no\s*concatenate\b",
    r"concatenate\b" + _P,
    r"(?:left|right|inner|outer)\s+join\b" + _P,
    r"join\b" + _P,
    r"(?:left|right|inner)\s+keep\b" + _P,
    r"mapping\b",
    r"generic\b",
    r"semantic\b",
    r"buffer\b" + _P,
    r"bundle\b(?:\s+info\b)?",
    r"image_size\s*\([^)]*\)",
    r"info\b",
    r"add\b(?:\s+only\b)?",
    r"replace\b(?:\s+only\b)?",
    r"merge\b(?:\s+only\b)?" + _P,
    r"crosstable\s*\([^)]*\)",
    r"hierarchy\s*\([^)]*\)",
    r"hierarchybelongsto\s*\([^)]*\)",
    r"intervalmatch\s*\([^)]*\)",
    r"first\s+\S+",
    r"sample\s+\S+",
)]

HEADS: list[tuple[re.Pattern[str], K]] = [(re.compile(p, re.IGNORECASE), k) for p, k in (
    (r"\$\((?:must_)?include\s*=", K.INCLUDE),
    (r"load\b", K.LOAD),
    (r"(?:sql\b|select\b)", K.SQL),
    (r"set\b", K.SET),
    (r"let\b", K.LET),
    (r"(?:lib\s+connect|connect|odbc\s+connect|oledb\s+connect|custom\s+connect)\b", K.CONNECT),
    (r"disconnect\b", K.DISCONNECT),
    (r"store\b", K.STORE),
    (r"drop\s+(?:tables?|fields?)\b", K.DROP),
    (r"rename\s+(?:tables?|fields?)\b", K.RENAME),
    (r"unqualify\b", K.UNQUALIFY),
    (r"qualify\b", K.QUALIFY),
    (r"elseif\b", K.ELSEIF),
    (r"else\b", K.ELSE),
    (r"end\s*if\b", K.END_IF),
    (r"if\b", K.IF),
    (r"for\b", K.FOR),
    (r"next\b", K.NEXT),
    (r"end\s*sub\b", K.END_SUB),
    (r"sub\b", K.SUB),
    (r"call\b", K.CALL),
    (r"do\b", K.DO),
    (r"loop\b", K.LOOP),
    (r"exit\b", K.EXIT),
    (r"end\s*switch\b", K.END_SWITCH),
    (r"switch\b", K.SWITCH),
    (r"case\b", K.CASE),
    (r"default\b", K.DEFAULT),
    (r"trace\b", K.TRACE),
    (r"sleep\b", K.SLEEP),
    (r"binary\b", K.BINARY),
    (r"section\b", K.SECTION),
    (r"map\b.+\busing\b", K.MAP_USING),
    (r"(?:search|tag|untag|comment|alias|directory|force|loosen|star|nullasvalue|nullasnull|execute|declare|"
     r"derive|unmap|unless|when)\b", K.DIRECTIVE),
    (r"[A-Za-z_][\w.]*\s*=", K.LET),  # LET is optional: "vX = 1;"
)]


def classify(raw: RawStatement, index: int) -> Statement:
    text = raw.text
    label = None
    m = LABEL.match(text)
    if m and not _starts_with_keyword_assignment(text):
        label = m.group("label").strip("[]\"")
        text = text[m.end():].lstrip()
    prefixes: list[str] = []
    while True:
        for p in PREFIXES:
            pm = p.match(text)
            if pm:
                prefixes.append(re.sub(r"\s+", " ", pm.group(0).strip()))
                text = text[pm.end():].lstrip()
                break
        else:
            # Qlik also accepts the label after a prefix: "LEFT JOIN (T) [Label]: LOAD ..."
            lm = LABEL.match(text) if prefixes and label is None else None
            if lm is None:
                break
            label = lm.group("label").strip("[]\"")
            text = text[lm.end():].lstrip()
    kind = K.OTHER
    for rx, k in HEADS:
        if rx.match(text):
            kind = k
            break
    if prefixes and kind not in (K.LOAD, K.SQL):
        kind = K.OTHER  # a load prefix must be followed by LOAD or SELECT
    return Statement(index=index, kind=kind, text=text, span=raw.span, label=label, prefixes=prefixes)


def _starts_with_keyword_assignment(text: str) -> bool:
    # "SET a = 'x:y'" or "LET a = ..." must never be read as a label
    return bool(re.match(r"(?i)\s*(set|let)\s", text))
