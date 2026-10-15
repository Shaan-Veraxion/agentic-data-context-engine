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

Reviewer decisions are learned from too (ADR-5, adce_core.feedback): SQL a reviewer wrote for a statement is a
memory entry (status reviewer) reused for the same statement in any application and shown as an example for similar
ones; a statement a reviewer accepted as a gap is not sent to the model again; and when a reviewer rejected a
table's SQL, answers learned before the rejection are not reused for that table: the model is asked again with the
rejected SQL and the reviewer's reason as a failed attempt.

repair_with_ai does the same for tables the rules translated but the data contradicts (agent 26's diagnosis): the
diagnose_mismatch route sees each statement's SQL and the failed comparisons and proposes a replacement for one
statement, accepted only when every comparison then passes. An accepted correction is remembered with
replaces_rule=True: later runs apply it to the same statement before the rules, and reports list it as a
candidate for a permanent rule.

With tools=True the model can also act before answering (gateway tool use, adce_gateway.tools): check_sql /
check_fix run the same checks and local validation the loop runs afterwards, get_statements shows the script's
other load statements, and kg_tools (read-only kg-mcp tools: context packs, similar facts) give what the knowledge
service knows. One gateway call, however many tool turns it takes, counts as one attempt.
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
from .runtime import MismatchFix, StatementTranslation

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
    replaces_rule: bool = False  # a validated correction of what the rules produce for this statement
    proven: bool = False  # passed on two or more apps' data and never failed (knowledge service: memory_check)
    learned_at: str = ""  # ISO time the knowledge service recorded it (compared with reviewer rejections)


def memory_key(statement: str, input_columns: list[str] | None) -> str:
    """Stable id of a memory entry: the normalised statement and its input columns."""
    import hashlib

    return hashlib.sha256((normalise(statement) + "|" + ",".join(input_columns or [])).encode()).hexdigest()[:24]


class TranslationMemory:
    """Accepted translations, shared across runs and apps. File-backed for the command line; agents keep the same
    entries as knowledge facts."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[MemoryEntry] = []
        # each reuse re-checks an entry on the current app's data; the results are kept as knowledge over time
        self.checks: list[dict[str, Any]] = []
        self.disproved: list[MemoryEntry] = []
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

    def rule_fix(self, statement: str) -> MemoryEntry | None:
        n = normalise(statement)
        for e in self.entries:
            if e.replaces_rule and e.status in ("ai_validated", "reviewer") and normalise(e.statement) == n:
                return e
        return None

    def drop(self, entry: MemoryEntry) -> None:
        """Stop using an entry in this run without disproving it (a reviewer rejected what it produced here)."""
        self.entries = [e for e in self.entries if e is not entry]

    def forget(self, entry: MemoryEntry) -> None:
        self.entries = [e for e in self.entries if e is not entry]
        self.disproved.append(entry)

    def checked(self, entry: MemoryEntry, app_id: str, passed: bool, level: str | None,
                problems: list[str] | None = None) -> None:
        self.checks.append({"key": memory_key(entry.statement, entry.input_columns), "statement": entry.statement,
                            "app_id": app_id, "passed": passed, "level": level, "problems": (problems or [])[:5]})

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
    status: str  # memory | ai_validated | ai_checked | declined | failed | skipped | gap_accepted | no_model | withheld
    sql: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    model_calls: int = 0  # gateway calls answered (live, memo or replay)
    tool_calls: int = 0  # tools the model called inside those calls


RELATIONS_NOTE = ("Tables of the script loaded before this statement that it names. Your SELECT may read them by "
                  "name (double-quoted, as written here) besides src, e.g. to reproduce MapSubString, ApplyMap, "
                  "Lookup or Exists. A mapping table maps its first column to its second. inline_rows are the "
                  "table's rows as written in the script.")


def static_problems(sql: str, input_columns: list[str] | None,
                    relations: dict[str, Any] | None = None) -> list[str]:
    """Checks that need no data: Dremio syntax, reads only src (and the other tables the statement names), uses
    known columns."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except ParseError as e:
        return [f"not valid Dremio SQL: {str(e).splitlines()[0]}"]
    if not isinstance(tree, exp.Select | exp.Union):
        return ["the answer must be one SELECT"]
    problems = []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    allowed = {"src", *ctes, *(n.lower() for n in relations or {})}
    others = sorted({t.sql(dialect=DIALECT) for t in tree.find_all(exp.Table)
                     if t.name.lower() not in allowed or t.db})
    if others:
        named = f" or {', '.join(relations)}" if relations else ""
        problems.append(f"reads tables other than src{named}: {', '.join(others)}")
    known: set[str] | None = None
    if input_columns is not None:
        known = {c.lower() for c in input_columns}
        for r in (relations or {}).values():
            if r.get("columns") is None:
                known = None
                break
            known |= {c.lower() for c in r["columns"]}
    if input_columns is not None and known is not None:
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
                            max_attempts: int = 4, max_rounds: int = 5, task: str = "ai_loop",
                            on_outcome: Callable[[Outcome], None] | None = None, tools: bool = False,
                            kg_tools: list[Any] | None = None, accepted_gaps: set[str] | None = None,
                            rejected: dict[str, dict[str, str]] | None = None) -> tuple[dict[int, str], list[Outcome]]:
    """accepted_gaps: spans a reviewer accepted as untranslated; rejected: table -> {reason, at} of a reviewer's
    rejection of the table's SQL."""
    overrides = dict(overrides or {})
    rejected = rejected or {}
    settings = _settings(app)
    outcomes: list[Outcome] = []
    for table in app.tables.values():  # corrections of rule output learned earlier, applied before the rules
        for step in table.steps:
            for op in step.ops:
                fix = memory.rule_fix(op.text)
                if fix is None or op.statement in overrides or _rejected_since(fix, rejected.get(table.name)):
                    continue
                # a correction proven on another app's data is re-checked on this app's data before it is used
                status = "memory_fix_unverified"
                level, problems = None, []
                if validator is not None:
                    t_fix = compose(app, {**overrides, op.statement: fix.sql})
                    if table.name in t_fix.tables and not t_fix.tables[table.name].untranslated:
                        level, problems = validator(t_fix, table.name)
                        status = "memory_fix_rejected" if problems else (
                            "memory_fix" if level in ("V3", "V4", "V5") else "memory_fix_unverified")
                o = Outcome(table.name, op.statement, f"{op.span.file}:{op.span.start_line}-{op.span.end_line}",
                            "learned correction of the rules", status, fix.sql)
                if status != "memory_fix_unverified":
                    memory.checked(fix, app.app_id, status == "memory_fix", level, problems)
                if status == "memory_fix_rejected":
                    memory.forget(fix)  # proven wrong on this app's data: not a general correction
                else:
                    overrides[op.statement] = fix.sql
                    fix.uses += 1
                outcomes.append(o)
                if on_outcome:
                    on_outcome(o)
    tried: set[int] = set(overrides)
    for _ in range(max_rounds):
        t = compose(app, overrides)
        todo = _roots(t, tried)
        if not todo:
            break
        stop = False
        for u in todo:
            tried.add(u.statement)
            if u.span in (accepted_gaps or ()):
                o = Outcome(u.table, u.statement, u.span, u.reason, "gap_accepted",
                            notes="a reviewer accepted this statement as untranslated")
                outcomes.append(o)
                if on_outcome:
                    on_outcome(o)
                continue
            o = await _one(app, u, overrides, gateway, memory, settings, validator, max_attempts, task,
                           tools, kg_tools, rejected.get(u.table))
            if o.status == "no_model" and not o.attempts:
                continue  # nothing learned for it and no model to ask: stays untranslated, nothing to report
            outcomes.append(o)
            if on_outcome:
                on_outcome(o)
            if o.sql and o.status in ("memory", "ai_validated", "ai_checked"):
                overrides[u.statement] = o.sql
            if o.status == "skipped":  # the provider is not answering: leave the rest for the next run
                stop = True
                break
        if stop:
            break
    return overrides, outcomes


async def _one(app: BIApp, u: Untranslated, overrides: dict[int, str], gateway: Any, memory: TranslationMemory,
               settings: dict[str, Any], validator: Validator | None, max_attempts: int, task: str,
               tools: bool = False, kg_tools: list[Any] | None = None,
               rejection: dict[str, str] | None = None) -> Outcome:
    o = Outcome(u.table, u.statement, u.span, u.reason, "failed")
    feedback: list[dict[str, Any]] = []
    hit = memory.exact(u.text, u.input_columns)
    if hit is not None and _rejected_since(hit, rejection):
        assert rejection is not None
        # a reviewer rejected this table's SQL after the answer was learned: ask again, with the reason
        memory.drop(hit)
        feedback.append({"sql": hit.sql, "problems": [f"a reviewer rejected the SQL of table {u.table}: "
                                                      f"{rejection.get('reason') or 'no reason given'}"]})
        o.attempts.append({"source": "memory", "sql": hit.sql, "rejected_by_reviewer": True})
        hit = None
    if hit is not None:
        ok, problems, level = _verify(app, u, hit.sql, overrides, validator)
        if level is not None or not ok:
            memory.checked(hit, app.app_id, ok, level, problems)
        if ok:
            hit.uses += 1
            o.status, o.sql = "memory", hit.sql
            o.attempts.append({"source": "memory", "level": level})
            return o
        memory.forget(hit)  # disproved by this app's evidence: never reuse it
        o.attempts.append({"source": "memory", "sql": hit.sql, "problems": problems})
    if gateway is None:  # AI off: only the memory answers
        o.status = "no_model"
        return o
    from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss
    from adce_gateway.privacy import PrivacyViolation

    for _attempt in range(max_attempts):
        inputs = {"statement": u.text, "why_rules_failed": u.reason, "input_relation": "src",
                  "input_columns": u.input_columns, "settings": settings,
                  "examples": [{"statement": e.statement, "input_columns": e.input_columns, "sql": e.sql}
                               | ({"written_by": "reviewer"} if e.status == "reviewer" else {})
                               for e in memory.similar(u.text)],
                  "previous_attempts": feedback}
        if u.relations:  # only when the statement names other tables, so earlier prompts stay the same
            inputs["other_relations"] = {"note": RELATIONS_NOTE, "tables": u.relations}
        kw: dict[str, Any] = {}
        if tools:
            inputs |= {"table_key": f"app:{app.app_id}/table:{u.table}", "app_key": f"app:{app.app_id}"}
            kw["tools"] = statement_tools(app, u, overrides, validator) + list(kg_tools or [])
        try:
            res = await gateway.call("translate_statement", inputs, StatementTranslation, task=task, **kw)
        except OutputInvalid as e:  # the model's output broke the schema: one attempt used, try again
            o.attempts.append({"error": f"OutputInvalid: {e}"})
            continue
        except PrivacyViolation as e:  # the input looked like data rows: never sent; stays for review
            o.attempts.append({"error": f"PrivacyViolation: {e}"})
            o.status = "withheld"
            return o
        except (ReplayMiss, GatewayRefusal, ProviderError, BudgetExceeded) as e:
            o.attempts.append({"error": f"{type(e).__name__}: {e}"})
            # provider unavailable (rate or daily limit, outage), no recording, or budget: not the model's failure
            o.status = "failed" if isinstance(e, GatewayRefusal) else "skipped"
            return o
        out = res.output
        assert isinstance(out, StatementTranslation)
        _count(o, res)
        o.notes = out.notes
        if not out.translatable or not out.sql.strip():
            o.status = "declined"
            o.attempts.append({"declined": out.notes})
            return o
        ok, problems, level = _verify(app, u, out.sql, overrides, validator)
        if any(normalise(f["sql"]) == normalise(out.sql) for f in feedback):
            problems = ["this SQL is identical to an earlier attempt that failed; change what the problems point to",
                        *problems]
        o.attempts.append({"sql": out.sql, "problems": problems, "level": level})
        if ok:
            o.status, o.sql = ("ai_validated" if level in ("V3", "V4", "V5") else "ai_checked"), out.sql
            memory.add(MemoryEntry(statement=u.text, input_columns=u.input_columns, sql=out.sql, status=o.status,
                                   app_id=app.app_id))
            return o
        feedback.append({"sql": out.sql, "problems": problems})
    return o


def _rejected_since(entry: MemoryEntry, rejection: dict[str, str] | None) -> bool:
    """A reviewer rejected the table's SQL after this answer was learned (or its learning time is unknown)."""
    return rejection is not None and entry.status != "reviewer" and \
        (not entry.learned_at or entry.learned_at <= rejection.get("at", ""))


def _settings(app: BIApp) -> dict[str, Any]:
    return {k: app.variables[k].value for k in SETTINGS if k in app.variables and app.variables[k].value}


def _statements(t: Translation, table: str) -> list[dict[str, Any]]:
    """Each statement of a table with the SQL it was translated to, its input shown as `src`."""
    out = []
    for c in t.ctes.values():
        if c.kind != "load" or len(c.statements) != 1 or not any(
                c.name in (s.chain_cte, s.result_cte) or c.statements[0] in s.statements
                for s in t.tables[table].steps):
            continue
        body = c.body.copy()
        cols = None
        for tb in body.find_all(exp.Table):
            if tb.alias == "src" and tb.name in t.ctes:
                cols = t.ctes[tb.name].columns
                tb.set("this", exp.to_identifier("src"))
                tb.set("alias", None)
        out.append({"statement": c.statements[0], "input_columns": cols,
                    "sql": body.sql(dialect=DIALECT, pretty=True)})
    return out


async def repair_with_ai(app: BIApp, gateway: Any, memory: TranslationMemory, validator: Validator, *,
                         overrides: dict[int, str] | None = None, tables: list[str] | None = None,
                         max_attempts: int = 3, task: str = "ai_repair",
                         on_outcome: Callable[[Outcome], None] | None = None, tools: bool = False,
                         kg_tools: list[Any] | None = None) -> tuple[dict[int, str], list[Outcome]]:
    """Agent 26's diagnosis: fix tables whose SQL runs but contradicts the evidence."""
    from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss
    from adce_gateway.privacy import PrivacyViolation

    overrides = dict(overrides or {})
    outcomes: list[Outcome] = []
    texts = {op.statement: op.text for x in app.tables.values() for s in x.steps for op in s.ops}
    for table in tables or list(app.tables):
        t = compose(app, overrides)
        r = t.tables.get(table)
        if r is None or r.untranslated or r.final_cte is None and not r.stores:
            continue
        level, problems = validator(t, table)
        if not problems:
            continue
        stmts = _statements(t, table)
        for s in stmts:
            s["text"] = texts.get(s["statement"], "")
        o = Outcome(table, -1, "", "; ".join(problems[:5]), "failed")
        feedback: list[dict[str, Any]] = []
        for _ in range(max_attempts):
            inputs: dict[str, Any] = {"table": table, "level": level, "statements": stmts,
                                      "failed_comparisons": problems, "settings": _settings(app),
                                      "previous_attempts": feedback}
            kw: dict[str, Any] = {}
            if tools:
                inputs |= {"table_key": f"app:{app.app_id}/table:{table}", "app_key": f"app:{app.app_id}"}
                kw["tools"] = repair_tools(app, table, stmts, overrides, validator) + list(kg_tools or [])
            try:
                res = await gateway.call("diagnose_mismatch", inputs, MismatchFix, task=task, **kw)
            except OutputInvalid as e:
                o.attempts.append({"error": f"OutputInvalid: {e}"})
                continue
            except PrivacyViolation as e:  # the input looked like data rows: never sent
                o.attempts.append({"error": f"PrivacyViolation: {e}"})
                o.status = "withheld"
                break
            except (ReplayMiss, GatewayRefusal, ProviderError, BudgetExceeded) as e:
                o.attempts.append({"error": f"{type(e).__name__}: {e}"})
                o.status = "failed" if isinstance(e, GatewayRefusal) else "skipped"
                break
            fix = res.output
            assert isinstance(fix, MismatchFix)
            _count(o, res)
            o.notes = f"{fix.cause}: {fix.explanation}"
            target = next((s for s in stmts if s["statement"] == fix.statement), None)
            if target is None or not fix.sql.strip():
                o.status = "declined"
                o.attempts.append({"declined": o.notes})
                break
            sid: int = target["statement"]
            bad = static_problems(fix.sql, target["input_columns"])
            if not bad:
                t2 = compose(app, {**overrides, sid: fix.sql})
                mine = [x.reason for x in t2.tables[table].untranslated]
                bad = mine or validator(t2, table)[1]
            if any(f["statement"] == sid and normalise(f["sql"]) == normalise(fix.sql) for f in feedback):
                bad = ["this SQL is identical to an earlier attempt that failed; change what the problems point to",
                       *bad]
            o.attempts.append({"statement": sid, "sql": fix.sql, "problems": bad})
            if not bad:
                overrides[sid] = fix.sql
                o.statement, o.status, o.sql = sid, "ai_repaired", fix.sql
                memory.add(MemoryEntry(statement=target["text"], input_columns=target["input_columns"], sql=fix.sql,
                                       status="ai_validated", app_id=app.app_id, replaces_rule=True))
                break
            feedback.append({"statement": sid, "sql": fix.sql, "problems": bad})
        outcomes.append(o)
        if on_outcome:
            on_outcome(o)
    return overrides, outcomes


def _verify(app: BIApp, u: Untranslated, sql: str, overrides: dict[int, str],
            validator: Validator | None) -> tuple[bool, list[str], str | None]:
    problems = static_problems(sql, u.input_columns, u.relations)
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


def _count(o: Outcome, res: Any) -> None:
    o.model_calls += 1
    o.tool_calls += len(getattr(res, "tool_calls", []) or [])


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str],
          run: Callable[[dict[str, Any]], Any]) -> Any:
    from adce_gateway.tools import Tool

    async def call(args: dict[str, Any]) -> Any:
        return run(args)

    return Tool(name, description, {"type": "object", "properties": properties, "required": required}, call)


def _statements_tool(app: BIApp) -> Any:
    ops = [(t.name, step.action.value, op) for t in app.tables.values() for step in t.steps for op in step.ops]

    def run(args: dict[str, Any]) -> Any:
        table = args.get("table")
        rows = [{"statement": op.statement, "table": name, "action": action, "text": op.text[:1500]}
                for name, action, op in sorted(ops, key=lambda x: x[2].statement) if not table or name == table]
        return {"statements": rows[:40], "more": max(0, len(rows) - 40)}

    return _tool("get_statements", "The script's load statements in execution order (statement number, table, "
                 "action, text); optionally only one table's.", {"table": {"type": "string"}}, [], run)


def statement_tools(app: BIApp, u: Untranslated, overrides: dict[int, str], validator: Validator | None) -> list[Any]:
    """check_sql: the checks and validation the answer will face; get_statements: script context."""

    def check(args: dict[str, Any]) -> Any:
        ok, problems, level = _verify(app, u, str(args.get("sql", "")), overrides, validator)
        out = {"ok": ok, "level": level, "problems": problems[:15], "validated_against_data": validator is not None}
        if ok:
            out["next"] = "this SQL passes: call submit_answer with it now"
        return out

    return [_tool("check_sql", "Run your candidate SQL (reading src) through the same checks and local data "
                  "validation your final answer will face. Returns ok, the level reached and the problems.",
                  {"sql": {"type": "string"}}, ["sql"], check), _statements_tool(app)]


def repair_tools(app: BIApp, table: str, stmts: list[dict[str, Any]], overrides: dict[int, str],
                 validator: Validator) -> list[Any]:
    def check(args: dict[str, Any]) -> Any:
        sid, sql = args.get("statement"), str(args.get("sql", ""))
        target = next((x for x in stmts if x["statement"] == sid), None)
        if target is None:
            return {"ok": False, "problems": [f"statement {sid} is not one of the table's statements"]}
        bad = static_problems(sql, target["input_columns"])
        if not bad:
            t2 = compose(app, {**overrides, target["statement"]: sql})
            bad = [x.reason for x in t2.tables[table].untranslated] or validator(t2, table)[1]
        return {"ok": not bad, "problems": bad[:15]} | ({"next": "this fix passes: call submit_answer with it now"}
                                                        if not bad else {})

    return [_tool("check_fix", "Replace one statement's SQL and run the table against its evidence again. "
                  "Returns ok and the comparisons that still fail.",
                  {"statement": {"type": "integer"}, "sql": {"type": "string"}}, ["statement", "sql"], check),
            _statements_tool(app)]


def memory_stats(checks: list[Any]) -> dict[str, dict[str, Any]]:
    """memory_check facts -> per memory key: apps where it passed and where it failed, and how often it was checked."""
    out: dict[str, dict[str, Any]] = {}
    for f in checks:
        v = f.value
        st = out.setdefault(f.subject.removeprefix("memory:"), {"passed_apps": set(), "failed_apps": set(),
                                                                "checks": 0})
        st["checks"] += 1
        (st["passed_apps"] if v.get("passed") else st["failed_apps"]).add(v.get("app_id"))
    return out


def gap_subjects(app: BIApp, t: Translation, outcomes: list[Outcome]) -> list[dict[str, Any]]:
    """What stays for a person after rules and model: loops modelled once (loop_not_unrolled), and root statements
    left untranslated at T3 or that the model declined or failed. Statements inside a loop's block (from its head
    to the next loop head, at most LOOP_LINES lines) are explained with the loop."""
    by_stmt = {o.statement: o for o in outcomes}
    loops, singles = [], []
    for i in app.issues:
        if i.code == "loop_not_unrolled" and i.span is not None:
            loops.append({"kind": "loop", "file": i.span.file, "line": i.span.start_line, "statement": i.statement,
                          "reason": i.message, "statements": [], "table": None})
    loops.sort(key=lambda x: (x["file"], x["line"]))
    for r in t.tables.values():
        for u in r.untranslated:
            if CASCADE.search(u.reason):
                continue
            o = by_stmt.get(u.statement)
            if u.tier != "T3" and not (o and o.status in ("declined", "failed")):
                continue
            file, _, lines = u.span.rpartition(":")
            start = int(lines.split("-")[0]) if lines.split("-")[0].isdigit() else 0
            item = {"statement": u.statement, "table": u.table, "reason": u.reason, "text": u.text[:2000],
                    "model_notes": o.notes if o else ""}
            home = None
            for k, lp in enumerate(loops):
                end = loops[k + 1]["line"] if k + 1 < len(loops) and loops[k + 1]["file"] == lp["file"] else 10**9
                if lp["file"] == file and lp["line"] <= start < min(end, lp["line"] + LOOP_LINES):
                    home = lp
            if home is not None:
                home["statements"].append(item)
            else:
                singles.append({"kind": "statement", "file": file, "line": start, **item, "statements": [item]})
    return loops + singles


LOOP_LINES = 80


def _block(scripts: dict[str, str], file: str, line: int, until: int | None) -> str:
    lines = scripts.get(file, "").splitlines()
    end = min(len(lines), (until - 1) if until else line + LOOP_LINES, line + LOOP_LINES)
    text = "\n".join(f"{n}: {lines[n - 1]}" for n in range(max(1, line - 3), end + 1) if n - 1 < len(lines))
    return text[:6000]


async def explain_gaps(app: BIApp, t: Translation, gateway: Any, scripts: dict[str, str],
                       outcomes: list[Outcome], *, task: str = "explain_gap",
                       answers: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The explain_gap route for each gap_subjects item: what the block does, how to do it in Dremio, the questions
    for the data owner and a SQL template. Script text and variable values only, never rows. `answers`: the data
    owner's answers to earlier questions, by gap (file:line), so the next analysis can be concrete."""
    from adce_gateway.gateway import BudgetExceeded, GatewayRefusal, OutputInvalid, ProviderError, ReplayMiss
    from adce_gateway.privacy import PrivacyViolation

    from .runtime import GapAnalysis

    subjects = gap_subjects(app, t, outcomes)
    heads = sorted((s["file"], s["line"]) for s in subjects)
    out = []
    for s in subjects:
        nxt = next((ln for f, ln in heads if f == s["file"] and ln > s["line"]), None)
        block = _block(scripts, s["file"], s["line"], nxt)
        names = sorted(set(re.findall(r"\$\((\w[\w.]*)\)", block)) | set(re.findall(r"(?i)\blet\s+(\w+)", block)))
        variables = {n: (app.variables[n].value if n in app.variables else None) for n in names[:30]}
        inputs = {"script_block": block, "pattern_hint": s["reason"],
                  "statements": [{k: v for k, v in x.items() if k != "text"} for x in s["statements"]],
                  "variables": variables,
                  "tables_in_script": sorted(app.tables)[:60]}
        given = (answers or {}).get(f"{s['file']}:{s['line']}")
        if given:  # only when the owner answered, so earlier prompts stay the same
            inputs["owner_answers"] = given
        try:
            res = await gateway.call("explain_gap", inputs, GapAnalysis, task=task)
        except (OutputInvalid, ReplayMiss, GatewayRefusal, ProviderError, BudgetExceeded, PrivacyViolation) as e:
            out.append({"kind": s["kind"], "span": f"{s['file']}:{s['line']}", "error": f"{type(e).__name__}: {e}"})
            continue
        a = res.output
        assert isinstance(a, GapAnalysis)
        out.append({"kind": s["kind"], "span": f"{s['file']}:{s['line']}", "statement": s["statement"],
                    "tables": sorted({x["table"] for x in s["statements"] if x.get("table")}),
                    "reason": s["reason"], **a.model_dump(), "model": getattr(res, "model", "")})
    return out
