"""The LLM pathway for statements the rules cannot translate: propose, check, validate, retry, learn.

For each statement the rules leave untranslated (root causes only; knock-on failures clear when their cause does):
1. Translation memory: the same statement with the same input columns was accepted before -> reuse its SQL.
2. Otherwise the model gateway route translate_statement proposes SQL, with accepted examples of similar
   statements from the memory.
3. Code checks the proposal: it parses as Dremio SQL, reads only `src`, uses only known input columns, and the
   table it belongs to passes the query checks; when a validator is given (local data, reload log, QVDs), the
   table must also run and reach V3 or better.
4. A failed check goes back to the model with the problems found, up to `max_attempts`.
5. Accepted SQL is stored in the memory (status ai_validated when data confirmed it, ai_checked otherwise) and
   becomes an example for later prompts and a direct answer for repeats. Declined or failed statements stay
   untranslated and go to review.
After each round the script is composed again with the accepted overrides: statements that only failed because an
earlier step failed are retried in the next round.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from adce_core.bi_ir import BIApp
from sqlglot import exp
from sqlglot.errors import ParseError

from .check import check_table
from .compose import DIALECT, Translation, Untranslated, compose
from .runtime import StatementTranslation

CASCADE = re.compile(r"depends on an untranslated step|table not loaded")
SETTINGS = ("ThousandSep", "DecimalSep", "MoneyThousandSep", "MoneyDecimalSep", "DateFormat", "TimestampFormat",
            "MonthNames", "FirstMonthOfYear")

# validator(translation, table) -> (level name, problems); level "V3"/"V4" counts as data-validated
Validator = Callable[[Translation, str], tuple[str, list[str]]]


def normalise(statement: str) -> str:
    """Comparable statement text: comments removed, whitespace collapsed (identifiers and literals kept)."""
    t = re.sub(r"//[^\n]*|/\*.*?\*/", " ", statement, flags=re.S)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class MemoryEntry:
    statement: str
    input_columns: list[str] | None
    sql: str
    status: str  # ai_validated | ai_checked | reviewer
    app_id: str = ""
    uses: int = 0


class TranslationMemory:
    """Accepted translations, shared across runs and apps. File-backed for the command line; agents keep the same
    entries as knowledge facts."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[MemoryEntry] = []
        if self.path and self.path.exists():
            self.entries = [MemoryEntry(**e) for e in json.loads(self.path.read_text(encoding="utf-8"))]

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(e) for e in self.entries], indent=2), encoding="utf-8")

    def exact(self, statement: str, cols: list[str] | None) -> MemoryEntry | None:
        n = normalise(statement)
        for e in self.entries:
            if normalise(e.statement) == n and (e.input_columns or None) == (cols or None):
                return e
        return None

    def similar(self, statement: str, k: int = 3) -> list[MemoryEntry]:
        words = set(re.findall(r"[A-Za-z_#]+\(|[A-Za-z_]+", statement.lower()))
        scored = []
        for e in self.entries:
            w = set(re.findall(r"[A-Za-z_#]+\(|[A-Za-z_]+", e.statement.lower()))
            if words | w:
                scored.append((len(words & w) / len(words | w), e))
        scored.sort(key=lambda x: -x[0])
        return [e for s, e in scored[:k] if s > 0.15]

    def forget(self, entry: MemoryEntry) -> None:
        self.entries = [e for e in self.entries if e is not entry]

    def add(self, entry: MemoryEntry) -> None:
        old = self.exact(entry.statement, entry.input_columns)
        if old is not None:
            rank = {"reviewer": 3, "ai_validated": 2, "ai_checked": 1}
            if rank.get(entry.status, 0) >= rank.get(old.status, 0):
                old.sql, old.status = entry.sql, entry.status
            return
        self.entries.append(entry)


@dataclass
class Outcome:
    table: str
    statement: int
    span: str
    reason: str
    status: str  # memory | ai_validated | ai_checked | declined | failed | skipped
    sql: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def static_problems(sql: str, input_columns: list[str] | None) -> list[str]:
    """Checks that need no data: Dremio syntax, reads only src, uses known columns."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except ParseError as e:
        return [f"not valid Dremio SQL: {str(e).splitlines()[0]}"]
    if not isinstance(tree, exp.Select | exp.Union):
        return ["the answer must be one SELECT"]
    problems = []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    others = sorted({t.sql(dialect=DIALECT) for t in tree.find_all(exp.Table)
                     if t.name.lower() != "src" and t.name.lower() not in ctes})
    if others:
        problems.append(f"reads tables other than src: {', '.join(others)}")
    if input_columns is not None:
        known = {c.lower() for c in input_columns}
        aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
        unknown = sorted({c.name for c in tree.find_all(exp.Column)
                          if c.name and c.name.lower() not in known and c.name.lower() not in aliases
                          and not isinstance(c.this, exp.Star)})
        if unknown:
            problems.append(f"columns not in src: {', '.join(unknown)} (src has: {', '.join(input_columns)})")
    return problems


def _roots(t: Translation, done: set[int]) -> list[Untranslated]:
    seen, out = set(), []
    for r in t.tables.values():
        for u in r.untranslated:
            if CASCADE.search(u.reason) or u.statement in done or u.statement in seen:
                continue
            seen.add(u.statement)
            out.append(u)
    return out


async def translate_with_ai(app: BIApp, gateway: Any, memory: TranslationMemory, *,
                            overrides: dict[int, str] | None = None, validator: Validator | None = None,
                            max_attempts: int = 3, max_rounds: int = 5, task: str = "ai_loop",
                            on_outcome: Callable[[Outcome], None] | None = None
                            ) -> tuple[dict[int, str], list[Outcome]]:
    overrides = dict(overrides or {})
    settings = {k: app.variables[k].value for k in SETTINGS if k in app.variables and app.variables[k].value}
    outcomes: list[Outcome] = []
    tried: set[int] = set(overrides)
    for _ in range(max_rounds):
        t = compose(app, overrides)
        todo = _roots(t, tried)
        if not todo:
            break
        for u in todo:
            tried.add(u.statement)
            o = await _one(app, u, overrides, gateway, memory, settings, validator, max_attempts, task)
            outcomes.append(o)
            if on_outcome:
                on_outcome(o)
            if o.sql and o.status in ("memory", "ai_validated", "ai_checked"):
                overrides[u.statement] = o.sql
    return overrides, outcomes


async def _one(app: BIApp, u: Untranslated, overrides: dict[int, str], gateway: Any, memory: TranslationMemory,
               settings: dict[str, Any], validator: Validator | None, max_attempts: int, task: str) -> Outcome:
    o = Outcome(u.table, u.statement, u.span, u.reason, "failed")
    hit = memory.exact(u.text, u.input_columns)
    if hit is not None:
        ok, problems, level = _verify(app, u, hit.sql, overrides, validator)
        if ok:
            hit.uses += 1
            o.status, o.sql = "memory", hit.sql
            o.attempts.append({"source": "memory", "level": level})
            return o
        memory.forget(hit)  # disproved by this app's evidence: never reuse it
        o.attempts.append({"source": "memory", "sql": hit.sql, "problems": problems})
    from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss

    feedback: list[dict[str, Any]] = []
    for _attempt in range(max_attempts):
        inputs = {"statement": u.text, "why_rules_failed": u.reason, "input_relation": "src",
                  "input_columns": u.input_columns, "settings": settings,
                  "examples": [{"statement": e.statement, "input_columns": e.input_columns, "sql": e.sql}
                               for e in memory.similar(u.text)],
                  "previous_attempts": feedback}
        try:
            res = await gateway.call("translate_statement", inputs, StatementTranslation, task=task)
        except (ReplayMiss, GatewayRefusal, OutputInvalid, ProviderError, BudgetExceeded) as e:
            o.attempts.append({"error": f"{type(e).__name__}: {e}"})
            o.status = "skipped" if isinstance(e, ReplayMiss | BudgetExceeded) else "failed"
            return o
        out = res.output
        assert isinstance(out, StatementTranslation)
        o.notes = out.notes
        if not out.translatable or not out.sql.strip():
            o.status = "declined"
            o.attempts.append({"declined": out.notes})
            return o
        ok, problems, level = _verify(app, u, out.sql, overrides, validator)
        o.attempts.append({"sql": out.sql, "problems": problems, "level": level})
        if ok:
            o.status, o.sql = ("ai_validated" if level in ("V3", "V4", "V5") else "ai_checked"), out.sql
            memory.add(MemoryEntry(statement=u.text, input_columns=u.input_columns, sql=out.sql, status=o.status,
                                   app_id=app.app_id))
            return o
        feedback.append({"sql": out.sql, "problems": problems})
    return o


def _verify(app: BIApp, u: Untranslated, sql: str, overrides: dict[int, str],
            validator: Validator | None) -> tuple[bool, list[str], str | None]:
    problems = static_problems(sql, u.input_columns)
    if problems:
        return False, problems, None
    t = compose(app, {**overrides, u.statement: sql})
    r = t.tables.get(u.table)
    if r is None:
        return False, ["table not produced"], None
    mine = [x for x in r.untranslated if x.statement == u.statement]
    if mine:
        return False, [x.reason for x in mine], None
    chk = check_table(t, u.table)
    blocking = [f"{x.name}: {x.message}" for x in chk.rules if x.blocking and not x.ok and x.name != "translated"]
    if blocking:
        return False, blocking, None
    if validator is None or r.untranslated:  # later steps of the table still untranslated: no data check yet
        return True, [], None
    level, data_problems = validator(t, u.table)
    if data_problems:  # any failed comparison (row counts, field values) rejects the answer, whatever the level
        return False, data_problems, level
    return True, [], level  # no failures: validated when the level is V3+, otherwise checked only
