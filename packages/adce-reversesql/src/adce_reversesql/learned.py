"""The translation memory as knowledge facts: load it for a run, and record what the run learned.

Agent 28 (statements the rules cannot translate) and agent 26 (corrections of rule output the data contradicts)
both learn answers; both read the memory the same way and record it the same way:

- translation_memory: one fact per learned answer (statement text, input columns, SQL, status); a later fact with
  status disproved retires it;
- memory_check: every reuse of an answer, re-checked on that application's data (passed or not, at which level).

An answer is proven when its re-checks passed on two or more applications and never failed. SQL a reviewer wrote
for a statement (translation_override, confirmed) joins the memory as status reviewer.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from adce_core.facts import Evidence, Fact, Trust

from .ai_loop import MemoryEntry, TranslationMemory, memory_key, memory_stats

Propose = Callable[[Fact], Awaitable[str]]


async def load_memory(kg: Any) -> TranslationMemory:
    """The latest entry per statement, without disproved ones, marked proven when its re-checks passed on two or
    more apps and never failed; plus reviewer SQL from any application."""
    latest: dict[str, Any] = {}
    for f in await kg.facts(kind="translation_memory"):
        latest[f.subject] = f
    stats = memory_stats(await kg.facts(kind="memory_check"))
    mem = TranslationMemory()
    for subject, f in latest.items():
        if f.value.get("status") != "disproved":
            e = MemoryEntry(**{k: f.value[k] for k in ("statement", "input_columns", "sql", "status", "app_id",
                                                       "replaces_rule") if k in f.value},
                            learned_at=f.recorded_at.isoformat())
            st = stats.get(subject.removeprefix("memory:"), {})
            confirmed = set(st.get("passed_apps", ())) | ({e.app_id} if e.status == "ai_validated" else set())
            e.proven = len(confirmed) >= 2 and not st.get("failed_apps")
            mem.entries.append(e)
    # SQL reviewers wrote for a statement, in any application: reused for the same statement elsewhere (checked
    # on that application's data like any memory answer) and shown as examples for similar statements
    for f in await kg.facts(kind="translation_override"):
        v = f.value
        if f.trust != Trust.CONFIRMED or not v.get("text"):
            continue
        key = f"memory:{memory_key(v['text'], v.get('input_columns'))}"
        if key in latest and latest[key].value.get("status") == "disproved":
            continue
        mem.add(MemoryEntry(statement=v["text"], input_columns=v.get("input_columns"), sql=v["sql"],
                            status="reviewer", app_id=f.subject.removeprefix("app:").split("/")[0],
                            learned_at=f.recorded_at.isoformat()))
    return mem


async def record_memory(propose: Propose, memory: TranslationMemory, known: set[int], app_id: str,
                        run_id: str) -> None:
    """Facts for what this run learned: new answers (entries not in `known`, the ids loaded at the start), every
    re-check of a reused answer, and answers the data disproved."""
    for e in memory.entries:
        if id(e) not in known:
            await propose(Fact(
                kind="translation_memory", subject=f"memory:{memory_key(e.statement, e.input_columns)}",
                trust=Trust.VALIDATED if e.status == "ai_validated" else Trust.INFERRED,
                value={"statement": e.statement, "input_columns": e.input_columns, "sql": e.sql, "status": e.status,
                       "app_id": e.app_id, "replaces_rule": e.replaces_rule},
                evidence=[Evidence(kind="query_result" if e.status == "ai_validated" else "rule_check",
                                   ref=f"{app_id}:{run_id}")]))
    for c in memory.checks:
        await propose(Fact(
            kind="memory_check", subject=f"memory:{c['key']}", trust=Trust.VALIDATED if c["passed"] and
            c["level"] in ("V3", "V4", "V5") else Trust.INFERRED,
            value={"app_id": app_id, "run_id": run_id, "passed": c["passed"], "level": c["level"],
                   "problems": c["problems"]},
            evidence=[Evidence(kind="query_result", ref=f"{app_id}:{run_id}")]))
    for e in memory.disproved:
        await propose(Fact(
            kind="translation_memory", subject=f"memory:{memory_key(e.statement, e.input_columns)}",
            trust=Trust.INFERRED, value={"statement": e.statement, "input_columns": e.input_columns,
                                         "sql": e.sql, "status": "disproved", "app_id": app_id,
                                         "replaces_rule": e.replaces_rule},
            evidence=[Evidence(kind="query_result", ref=f"{app_id}:{run_id}")]))
    memory.checks.clear()  # recorded: a second call in the same run records only what came after
    memory.disproved.clear()
