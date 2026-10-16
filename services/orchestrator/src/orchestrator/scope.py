"""Which tables a change affects, so a re-run validates only those (and what depends on them).

An event names what changed: a table (reviewer SQL, a rejection, a gap's tables), a statement text (reviewer SQL
reused in another application, a disproved learned answer), a variable or a source. `hints_from_events` turns a
batch of outbox events into hints per application; `tables_in_scope` resolves the hints against the application's
BI IR and adds every table downstream (tables that read them RESIDENT, join or keep against them, use them as a
mapping, or load a QVD they store). A hint that matches nothing makes the scope the whole application, so a re-run
never skips a table it should check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from adce_core.bi_ir import BIApp
from adce_core.ports import KGPort
from adce_reversesql.ai_loop import normalise


@dataclass
class Hints:
    tables: set[str] = field(default_factory=set)
    statements: set[str] = field(default_factory=set)  # normalised statement texts
    variables: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)  # source subjects (source:<conn>:<path>, qvd:..., source::<file>)
    everything: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"tables": sorted(self.tables), "statements": sorted(self.statements),
                "variables": sorted(self.variables), "sources": sorted(self.sources), "everything": self.everything}

    @classmethod
    def of(cls, d: dict[str, Any] | None) -> Hints:
        d = d or {}
        return cls(set(d.get("tables", [])), set(d.get("statements", [])), set(d.get("variables", [])),
                   set(d.get("sources", [])), bool(d.get("everything")))


def _app(subject: str) -> str | None:
    return subject.removeprefix("app:").split("/")[0] if subject.startswith("app:") else None


def _table(subject: str) -> str | None:
    return subject.split("/table:", 1)[1] if "/table:" in subject else None


async def hints_from_events(kg: KGPort, events: list[dict[str, Any]], apps: dict[str, list[str]]) -> dict[str, Hints]:
    """Per application affected (events.affected), what the events changed."""
    out: dict[str, Hints] = {a: Hints() for a in apps}
    for e in events:
        fact = (e.get("payload") or {}).get("fact")
        if not fact:
            continue
        kind, subject, value, topic = fact["kind"], fact["subject"], fact.get("value", {}), e["topic"]
        app = _app(subject)
        if topic == "fact.confirmed" and kind == "translation_override":
            if app in out and _table(subject):
                out[app].tables.add(_table(subject) or "")
            if value.get("text"):
                for a in out:
                    if a != app:
                        out[a].statements.add(normalise(value["text"]))
        elif topic == "fact.confirmed" and kind == "variable_value" and app in out:
            out[app].variables.add(subject.rsplit("/var:", 1)[-1])
        elif topic == "fact.confirmed" and kind == "gap_answer" and app in out:
            gaps = [f for f in await kg.facts(kind="gap_analysis", subject=subject)]
            tables = {t for f in gaps for t in f.value.get("tables", [])}
            if tables:
                out[app].tables |= tables
            else:
                out[app].everything = True
        elif topic == "fact.confirmed" and kind == "resolves_to":
            for a in out:
                out[a].sources.add(subject)
        elif topic == "fact.rejected" and kind == "report_sql" and app in out and _table(subject):
            out[app].tables.add(_table(subject) or "")
        elif topic == "fact.proposed" and kind == "translation_memory" and value.get("status") == "disproved":
            for a in out:
                out[a].statements.add(normalise(value.get("statement", "")))
    for h in out.values():
        if not (h.tables or h.statements or h.variables or h.sources):
            h.everything = True
    return out


def _source_matches(src_subject: str, op: Any) -> bool:
    s = op.source
    if src_subject.startswith("source::") or src_subject.startswith("qvd:"):
        ref = src_subject.split(":", 2)[-1] if src_subject.startswith("source::") else src_subject[4:]
        return bool(s.ref) and PurePosixPath(str(s.ref).replace("\\", "/")).name.lower() == \
            PurePosixPath(ref.replace("\\", "/")).name.lower()
    if src_subject.startswith("source:"):
        path = src_subject.split(":", 2)[2].lower().replace('"', "")
        return any(path.endswith(t.lower().replace('"', "")) or t.lower().replace('"', "").endswith(path)
                   for t in s.tables)
    return False


def downstream(app: BIApp, tables: set[str]) -> set[str]:
    """The tables, plus every table that reads one of them (RESIDENT, join/keep target or other table, ApplyMap
    mapping, a QVD it stores), transitively."""
    stores = {PurePosixPath(st.target.replace("\\", "/")).name.lower(): st.table for st in app.stores}
    reads: dict[str, set[str]] = {}
    for name, t in app.tables.items():
        for step in t.steps:
            for op in step.ops:
                src = op.source
                if src.kind == "resident" and src.ref:
                    reads.setdefault(src.ref, set()).add(name)
                if src.kind == "qvd" and src.ref:
                    producer = stores.get(PurePosixPath(src.ref.replace("\\", "/")).name.lower())
                    if producer:
                        reads.setdefault(producer, set()).add(name)
                for m in re.findall(r"(?i)applymap\s*\(\s*'([^']+)'", op.text):
                    reads.setdefault(m, set()).add(name)
            if step.other_table:
                reads.setdefault(step.other_table, set()).add(name)
    out, todo = set(tables), list(tables)
    while todo:
        for n in reads.get(todo.pop(), set()):
            if n not in out:
                out.add(n)
                todo.append(n)
    return out


def tables_in_scope(app: BIApp, statements_raw: dict[int, str], hints: Hints) -> list[str] | None:
    """The tables a re-run must validate, or None for every table. statements_raw: executed statement index ->
    its text as written (with $(variables)), from the BI IR document."""
    if hints.everything:
        return None
    hit: set[str] = {t for t in hints.tables if t in app.tables}
    for name, t in app.tables.items():
        for step in t.steps:
            for op in step.ops:
                raw = statements_raw.get(op.statement, op.text)
                if hints.statements and normalise(op.text) in hints.statements:
                    hit.add(name)
                if any(f"$({v})" in raw for v in hints.variables):
                    hit.add(name)
                if any(_source_matches(s, op) for s in hints.sources):
                    hit.add(name)
    if not hit:
        return None  # nothing matched: check everything rather than skip something
    return sorted(downstream(app, hit))
