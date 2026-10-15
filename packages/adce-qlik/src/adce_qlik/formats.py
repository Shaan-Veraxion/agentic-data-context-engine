"""Qlik date format codes (Date#, Date, DateFormat) converted to strftime and to Dremio TO_DATE formats.

Only the date codes with a fixed width or an unambiguous name are converted: YYYY, YY, MMMM, MMM, MM, DD, with
separators passed through. Anything else (single M or D, week days, time codes) is refused, so callers report it
instead of guessing. Month names are the English ones (Qlik takes them from the MonthNames variable).
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"YYYY|YY|MMMM|MMM|MM|DD|[-/.,: ]+|.")
_STRFTIME = {"YYYY": "%Y", "YY": "%y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "DD": "%d"}
_DREMIO = {"YYYY": "YYYY", "YY": "YY", "MMMM": "MONTH", "MMM": "MON", "MM": "MM", "DD": "DD"}


class UnsupportedFormat(ValueError):
    pass


def _convert(fmt: str, table: dict[str, str]) -> str:
    out = []
    for tok in _TOKEN.findall(fmt):
        if tok in table:
            out.append(table[tok])
        elif re.fullmatch(r"[-/.,: ]+", tok):
            out.append(tok)
        else:
            raise UnsupportedFormat(f"date format code {tok!r} in {fmt!r}")
    return "".join(out)


def to_strftime(fmt: str) -> str:
    return _convert(fmt, _STRFTIME)


def to_dremio(fmt: str) -> str:
    return _convert(fmt, _DREMIO)
