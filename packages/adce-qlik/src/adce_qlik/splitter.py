"""Split a Qlik load script into statements with exact source spans.

Rules (QlikView and Qlik Sense):
- A statement ends at ';' outside strings ('…', "…", `…`), [bracketed names], $(…) expansions and comments.
- Control statements (IF, ELSE, FOR, NEXT, SUB, DO, LOOP, SWITCH, CASE, CALL, EXIT, ...) end at ';' or end of line.
- Comments: // to end of line (but not the '//' in 'lib://'), /* … */, and REM … ; at the start of a statement.
- Anything left without a terminating ';' at the end of the file is still emitted, flagged as unterminated.
Comments are blanked to spaces in the statement text, so offsets and line numbers stay exact.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from adce_core.bi_ir import SourceSpan

CONTROL_START = re.compile(
    r"(?i)(if|elseif|else|end\s*if|for|next|sub|end\s*sub|do|loop|exit|switch|case|default|end\s*switch|call)\b")
REM_START = re.compile(r"(?i)rem\b")


@dataclass
class RawStatement:
    text: str  # comments blanked, terminator removed, stripped
    raw: str  # original text between start and terminator
    span: SourceSpan
    terminated: bool


def _skip_quoted(s: str, i: int, q: str) -> int:
    """i points at the opening quote; returns index after the closing quote. Doubled quotes are escapes."""
    j = i + 1
    n = len(s)
    close = "]" if q == "[" else q
    while j < n:
        if s[j] == close:
            if j + 1 < n and s[j + 1] == close:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _skip_dollar(s: str, i: int) -> int:
    """i points at '$('; returns index after the matching ')'."""
    depth = 0
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if c in "'\"":
            j = _skip_quoted(s, j, c)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def blank_comments(s: str) -> str:
    """Replace comments with spaces (newlines kept) so offsets are preserved."""
    out = list(s)
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"`[":
            i = _skip_quoted(s, i, c)
            continue
        if s.startswith("$(", i):
            i = _skip_dollar(s, i)
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if s.startswith("//", i) and not (i > 0 and s[i - 1] == ":"):
            j = s.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


class _Lines:
    def __init__(self, s: str) -> None:
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", s)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def split_script(script: str, file: str = "script.qvs") -> list[RawStatement]:
    script = script.replace("\r\n", "\n").lstrip("﻿")
    clean = blank_comments(script)
    lines = _Lines(script)
    out: list[RawStatement] = []
    i, n = 0, len(clean)
    start: int | None = None
    control = False

    def emit(a: int, b: int, terminated: bool) -> None:
        raw = script[a:b]
        text = clean[a:b].strip()
        if not text:
            return
        lead = len(clean[a:b]) - len(clean[a:b].lstrip())
        a2 = a + lead
        b2 = a2 + len(text)
        out.append(RawStatement(text=text, raw=raw.strip(), terminated=terminated, span=SourceSpan(
            file=file, start_line=lines.line(a2), end_line=lines.line(max(a2, b2 - 1)), start_offset=a2,
            end_offset=b2)))

    while i < n:
        c = clean[i]
        if start is None:
            if c.isspace():
                i += 1
                continue
            if REM_START.match(clean, i):
                j = clean.find(";", i)
                i = n if j < 0 else j + 1
                continue
            start = i
            control = bool(CONTROL_START.match(clean, i))
        if c in "'\"`[":
            i = _skip_quoted(clean, i, c)
            continue
        if clean.startswith("$(", i):
            i = _skip_dollar(clean, i)
            continue
        if c == ";":
            emit(start, i, True)
            start = None
            i += 1
            continue
        if control and c == "\n":
            emit(start, i, True)
            start = None
        i += 1
    if start is not None:
        emit(start, n, False)
    return out
