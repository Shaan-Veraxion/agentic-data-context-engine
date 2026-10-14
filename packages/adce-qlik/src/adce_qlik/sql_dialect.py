"""Which SQL dialect an embedded SQL statement is written in.

Qlik sends the text after SQL/SELECT to the connected database unchanged, so it is written in that database's
dialect: Oracle, SQL Server, Dremio and others. The dialect decides how it is parsed and transpiled to Dremio.

Order of evidence: an explicit setting for the connection (connections.json or a reviewer's answer), what the
connection's earlier statements showed, the connection name, then features of the SQL text. The result names a
sqlglot dialect.
"""
from __future__ import annotations

import re

DIALECTS = ("dremio", "oracle", "tsql", "postgres", "mysql", "snowflake", "bigquery", "databricks")

_TEXT_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("oracle", re.compile(r"(?i)\b(rawtohex|hextoraw|sysdate|systimestamp|nvl2?|decode|rownum|to_date|to_char|"
                          r"to_number|trunc)\s*[(\s]|\bfrom\s+dual\b|\b(sysdate|rownum)\b")),
    ("tsql", re.compile(r"(?i)\bdbo\.|\bgetdate\s*\(|\bisnull\s*\(|\bselect\s+(distinct\s+)?top\s+\d|"
                        r"\bwith\s*\(\s*nolock\s*\)|\bdatediff\s*\(\s*(day|month|year|hour|minute|dd|mm|yy)\b|"
                        r"\bconvert\s*\(\s*(n?varchar|date|datetime|int)|\bas\s+'[^']+'")),
]
_NAME_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("dremio", re.compile(r"(?i)dremio")),
    ("oracle", re.compile(r"(?i)oracle")),
    ("tsql", re.compile(r"(?i)\b(mssql|sql\s*server|sqlserver)\b")),
    ("snowflake", re.compile(r"(?i)snowflake")),
    ("postgres", re.compile(r"(?i)postgres")),
]


def detect(sql: str, connection: str | None, *, configured: dict[str, str] | None = None,
           learned: dict[str, str] | None = None) -> tuple[str, str]:
    """Returns (dialect, how it was decided)."""
    conf = {k.lower(): v for k, v in (configured or {}).items()}
    if connection and connection.lower() in conf:
        return conf[connection.lower()], "configured"
    for d, rx in _TEXT_HINTS:
        if rx.search(sql):
            return d, "sql text"
    if connection and learned and connection in learned:
        return learned[connection], "earlier statements on this connection"
    if connection:
        for d, rx in _NAME_HINTS:
            if rx.search(connection):
                return d, "connection name"
    return "dremio", "default"
