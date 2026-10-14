"""Measures for one application, matching the starting-stage targets: statements understood, statements translated,
tables complete, and what blocks the rest (root causes, not their knock-on effects)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from adce_core.bi_ir import BIApp

from .compose import Translation

CASCADE = re.compile(r"depends on an untranslated step")


@dataclass
class AppMeasures:
    app_id: str
    statements: int
    statements_understood: int
    table_statements: int  # statements that load, join, concatenate, ... a table
    table_statements_translated: int
    tables: int  # output tables: left in the data model or stored to QVD
    tables_complete: int  # output tables whose every step translated
    root_causes: list[tuple[str, str, int]] = field(default_factory=list)  # (tier, reason, statements)
    knock_on: int = 0  # statements blocked only because an earlier step of the table was not translated
    issues: dict[str, int] = field(default_factory=dict)
    connection_dialects: dict[str, str] = field(default_factory=dict)
    unresolved_variables: list[str] = field(default_factory=list)
    inputs_to_resolve: int = 0
    ai: dict[str, int] = field(default_factory=dict)  # LLM pathway outcomes: memory, ai_validated, ai_checked, ...

    @property
    def understood_ratio(self) -> float:
        return self.statements_understood / self.statements if self.statements else 1.0

    @property
    def translated_ratio(self) -> float:
        return self.table_statements_translated / self.table_statements if self.table_statements else 1.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"understood_ratio": round(self.understood_ratio, 4),
                               "translated_ratio": round(self.translated_ratio, 4)}


def _normalise(reason: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", reason)[:100]


def measure(app: BIApp, t: Translation, outcomes: list[Any] | None = None) -> AppMeasures:
    done: set[int] = set()
    blocked: set[int] = set()
    causes: Counter[tuple[str, str]] = Counter()
    knock_on = 0
    for r in t.tables.values():
        for s in r.steps:
            done.update(s.statements)
        for u in r.untranslated:
            blocked.add(u.statement)
            if CASCADE.search(u.reason):
                knock_on += 1
            else:
                causes[(u.tier, _normalise(u.reason))] += 1
    # loads inside a loop that could not be unrolled ran once in the model: the SQL covers one iteration only
    looped = {o.statement for x in app.tables.values() for st in x.steps for o in st.ops
              if any("not unrolled" in c for c in st.context)}
    if looped & done:
        reason = "load inside a loop that could not be unrolled (data-dependent): one iteration modelled"
        causes[("T3", reason)] += len(looped & done)
        blocked |= looped & done
    table_statements = done | blocked
    unresolved = sorted({v for i in app.issues if i.code == "unresolved_variable"
                         for v in i.message.removeprefix("unresolved: ").split(", ")})
    inputs = {i for r in t.tables.values() for i in r.inputs}
    # output tables: what the script leaves in the data model or stores to QVD
    outputs = [n for n, x in app.tables.items() if not x.mapping and (x.dropped_at is None or x.stored_to)]
    return AppMeasures(
        app_id=app.app_id, statements=app.coverage.total, statements_understood=app.coverage.understood,
        table_statements=len(table_statements), table_statements_translated=len(table_statements - blocked),
        tables=len(outputs), tables_complete=sum(1 for n in outputs if n in t.tables and _done(t, n, looped)),
        root_causes=[(tier, reason, n) for (tier, reason), n in causes.most_common()], knock_on=knock_on,
        issues=dict(Counter(i.code for i in app.issues).most_common()),
        connection_dialects=dict(app.connection_dialects), unresolved_variables=unresolved,
        inputs_to_resolve=len(inputs) + len(app.sources),
        ai=dict(Counter(o.status for o in outcomes or [])))


def _done(t: Translation, table: str, looped: set[int]) -> bool:
    r = t.tables[table]
    if r.untranslated or (r.final_cte is None and not r.stores):
        return False
    # the table's SQL must not depend, directly or through other tables, on a load modelled for one loop iteration
    todo, seen = [c for c in [r.final_cte, *(c for _, c in r.stores)] if c], set()
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        if set(t.ctes[c].statements) & looped:
            return False
        todo += t.ctes[c].deps
    return True
