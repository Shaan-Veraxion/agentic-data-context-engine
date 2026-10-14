"""Query checks for composed table SQL (agent 25, rule part). A table passing every blocking rule reaches V1."""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot.errors import ParseError

from .compose import DIALECT, Translation


@dataclass
class Rule:
    name: str
    ok: bool
    message: str = ""
    blocking: bool = True


@dataclass
class CheckResult:
    table: str
    rules: list[Rule] = field(default_factory=list)
    sql: str | None = None

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rules if r.blocking)

    def failed(self) -> list[Rule]:
        return [r for r in self.rules if not r.ok]


def check_table(t: Translation, table: str) -> CheckResult:
    r = t.tables[table]
    res = CheckResult(table=table)
    res.rules.append(Rule("translated", not r.untranslated,
                          "; ".join(f"stmt {u.statement}: {u.reason} ({u.tier})" for u in r.untranslated)))
    sql = t.table_sql(table)
    res.sql = sql
    if sql is None:
        res.rules.append(Rule("composed", False, "no final SQL (dropped table or untranslated steps)"))
        return res
    try:
        sqlglot.parse_one(sql, read=DIALECT)
        res.rules.append(Rule("dremio_syntax", True))
    except ParseError as e:
        res.rules.append(Rule("dremio_syntax", False, str(e).splitlines()[0]))
    try:
        sqlglot.transpile(sql, read=DIALECT, write="duckdb")
        res.rules.append(Rule("transpiles_for_local_run", True))
    except Exception as e:  # noqa: BLE001 - any transpile failure blocks the local run
        res.rules.append(Rule("transpiles_for_local_run", False, str(e).splitlines()[0]))
    unknown = f"fields not found in their input: {', '.join(r.unknown_fields)}" if r.unknown_fields else ""
    res.rules.append(Rule("known_fields", not r.unknown_fields, unknown))
    nondet = [n for n in r.notes if "not deterministic" in n]
    res.rules.append(Rule("deterministic", not nondet, "; ".join(nondet), blocking=False))
    res.rules.append(Rule("inputs_in_dremio", not r.inputs,
                          f"QVD/file inputs to resolve through QVD chains or the catalog: {', '.join(r.inputs)}"
                          if r.inputs else "", blocking=False))
    ai = [c for c in t.ctes.values() if c.ai and c.name in sql]
    res.rules.append(Rule("rules_only", not ai, "contains AI-assisted CTEs: review required" if ai else "",
                          blocking=False))
    return res
