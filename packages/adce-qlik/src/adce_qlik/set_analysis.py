"""Parse Qlik set analysis: {<Field={values}, Field-={values}>}, {$<...>}, {1}, {1<...>}.

Supported: identifiers $ and 1, modifiers with = += -= *= /=, element lists of numbers, 'literals' and
"search strings" (wildcards * and ?, or a comparison such as ">=2020"), and an empty list (F=) that clears a field.
Not supported (reported as unsupported, never guessed): bookmarks and alternate states as identifiers, set
operators between sets (+ - * /), element functions P() and E(), advanced searches ("=expression"), dollar
expansions left unexpanded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field


class SetParseError(ValueError):
    pass


@dataclass
class SetValue:
    kind: str  # literal | search
    value: str | int | float


@dataclass
class SetModifier:
    field: str
    op: str  # = += -= *= /=
    values: list[SetValue] = dc_field(default_factory=list)


@dataclass
class SetExpression:
    identifier: str  # "$" (current selections) or "1" (all data)
    modifiers: list[SetModifier] = dc_field(default_factory=list)
    raw: str = ""


_FIELD = r"(\[[^\]]+\]|\"[^\"]+\"|[A-Za-z_À-￿][\w.%#@À-￿]*)"
_MOD = re.compile(rf"\s*{_FIELD}\s*([+\-*/]?=)\s*")


def parse_set(raw: str) -> SetExpression:
    s = raw.strip()
    if not (s.startswith("{") and s.endswith("}")):
        raise SetParseError("set expression must be enclosed in { }")
    body = s[1:-1].strip()
    if "$(" in body:
        raise SetParseError("unexpanded dollar expansion in set expression")
    m = re.match(r"^(\$|1)?\s*", body)
    ident = (m.group(1) if m else None) or "$"
    rest = body[m.end():] if m else body
    if not rest:
        return SetExpression(identifier=ident, raw=raw)
    if not (rest.startswith("<") and rest.endswith(">")):
        raise SetParseError("only {<modifiers>} with $ or 1 is supported (no bookmarks, states or set operators)")
    out = SetExpression(identifier=ident, raw=raw)
    for part in _split_modifiers(rest[1:-1]):
        mm = _MOD.match(part)
        if not mm:
            raise SetParseError(f"modifier not understood: {part.strip()!r}")
        name = mm.group(1).strip("[]\"")
        value_text = part[mm.end():].strip()
        out.modifiers.append(SetModifier(field=name, op=mm.group(2), values=_values(value_text)))
    return out


def _split_modifiers(text: str) -> list[str]:
    parts, depth, cur, q = [], 0, "", ""
    for ch in text:
        if q:
            cur += ch
            q = "" if ch == q else q
            continue
        if ch in "'\"[":
            q = "]" if ch == "[" else ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _values(text: str) -> list[SetValue]:
    if text == "":
        return []  # "F=" clears the field
    if re.match(r"(?i)^[PE]\s*\(", text):
        raise SetParseError("element functions P() / E() are not supported")
    if not (text.startswith("{") and text.endswith("}")):
        raise SetParseError(f"element set must be in {{ }}: {text!r}")
    inner = text[1:-1].strip()
    if not inner:
        return []
    out: list[SetValue] = []
    for tok in _split_modifiers(inner):
        t = tok.strip()
        if t.startswith("'") and t.endswith("'"):
            out.append(SetValue("literal", t[1:-1].replace("''", "'")))
        elif t.startswith('"') and t.endswith('"'):
            v = t[1:-1]
            if v.startswith("="):
                raise SetParseError("advanced search (\"=expression\") is not supported")
            out.append(SetValue("search", v))
        elif re.fullmatch(r"-?\d+", t):
            out.append(SetValue("literal", int(t)))
        elif re.fullmatch(r"-?\d*\.\d+", t):
            out.append(SetValue("literal", float(t)))
        else:
            out.append(SetValue("literal", t))  # unquoted text value
    return out
