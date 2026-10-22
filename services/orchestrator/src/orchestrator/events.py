"""Event-driven re-runs (ADR-6): the orchestrator follows the knowledge outbox and re-runs the applications an event
affects, instead of waiting for the scheduled re-run.

kg-mcp writes every fact and its outbox event in one transaction (adce_kg.fact_log); this consumer reads the outbox
after its saved position, works out which applications each event affects, and re-runs each of them once per batch
(debounced per application) from their stored uploads:

| Event                                   | Re-runs                                                          |
|-----------------------------------------|------------------------------------------------------------------|
| fact.confirmed translation_override     | that application, and every other one with the same statement   |
|                                         | still untranslated (the reviewer's SQL is reused there)          |
| fact.confirmed variable_value           | that application (the variable's value changes the script)       |
| fact.confirmed gap_answer               | that application (the gap is analysed again with the answers)    |
| fact.confirmed resolves_to              | every application reading that source                            |
| fact.rejected report_sql                | that application (the model is asked again, with the reason)     |
| fact.proposed translation_memory        | the applications that relied on the answer (learned or passed    |
|   with status disproved                 | there): they stop using it                                       |

Each re-run enters the graph at the first node the change affects, with the last run's summary (orchestrator
pipeline, re-entry): a variable's value at extract (the script is read again), a source mapping at resolve (the
translation is reused: only the SQL's source names change), everything else at translate. Without an earlier
run the application runs from the start.

Each re-run validates only the tables the change affects and those downstream (orchestrator.scope); the other
tables keep the previous run's validation.

Re-runs do not loop: facts are content-addressed, so a re-run that reaches the same conclusions writes nothing new
and raises no event. Re-runs reuse memoized model answers, so they cost no model calls unless something changed.

    uv run python -m orchestrator.events once        # handle what is pending, then stop
    uv run python -m orchestrator.events run         # follow the outbox (Kubernetes: orchestrator-events)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Protocol

from adce_core.ports import KGPort
from adce_reversesql.ai_loop import memory_key, normalise

from .learning import rerun_all
from .scope import hints_from_events

log = logging.getLogger("adce.events")


class Cursor(Protocol):
    def get(self, name: str) -> int: ...

    def set(self, name: str, seq: int) -> None: ...


class MemoryCursor:
    def __init__(self) -> None:
        self.pos: dict[str, int] = {}

    def get(self, name: str) -> int:
        return self.pos.get(name, 0)

    def set(self, name: str, seq: int) -> None:
        self.pos[name] = seq


class PostgresCursor:
    """Consumer positions in table event_cursor (migration 0003)."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self.con = psycopg.connect(dsn, autocommit=True)

    def get(self, name: str) -> int:
        row = self.con.execute("SELECT seq FROM event_cursor WHERE consumer = %s", (name,)).fetchone()
        return int(row[0]) if row else 0

    def set(self, name: str, seq: int) -> None:
        self.con.execute("INSERT INTO event_cursor (consumer, seq) VALUES (%s, %s) ON CONFLICT (consumer) DO "
                         "UPDATE SET seq = EXCLUDED.seq, updated_at = now()", (name, seq))

    def close(self) -> None:
        self.con.close()


def _app(subject: str) -> str | None:
    return subject.removeprefix("app:").split("/")[0] if subject.startswith("app:") else None


async def affected(kg: KGPort, events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Application -> why it should run again, for a batch of outbox events."""
    out: dict[str, list[str]] = {}

    def add(app: str | None, why: str) -> None:
        if app and why not in out.setdefault(app, []):
            out[app].append(why)

    untranslated: dict[str, set[str]] | None = None
    for e in events:
        fact = e.get("payload", {}).get("fact")
        if not fact:
            continue
        kind, subject, value, topic = fact["kind"], fact["subject"], fact.get("value", {}), e["topic"]
        if topic == "fact.confirmed" and kind == "translation_override":
            add(_app(subject), "reviewer SQL")
            if value.get("text"):
                if untranslated is None:  # statement text -> applications where it is still untranslated
                    untranslated = {}
                    for f in await kg.facts(kind="bi_transform"):
                        for u in f.value.get("untranslated", []):
                            if u.get("text"):
                                untranslated.setdefault(normalise(u["text"]), set()).add(_app(f.subject) or "")
                for other in sorted(untranslated.get(normalise(value["text"]), set()) - {_app(subject)}):
                    add(other, "reviewer SQL for the same statement")
        elif topic == "fact.confirmed" and kind == "variable_value":
            add(_app(subject), "variable value")
        elif topic == "fact.confirmed" and kind == "gap_answer":
            add(_app(subject), "owner answered questions about a gap")
        elif topic == "fact.confirmed" and kind == "resolves_to":
            for f in await kg.facts(kind="reads_source"):
                if f.object == subject:
                    add(_app(f.subject), f"source mapped: {subject}")
        elif topic == "fact.rejected" and kind == "report_sql":
            add(_app(subject), "reviewer rejected SQL")
        elif topic == "fact.proposed" and kind == "translation_memory" and value.get("status") == "disproved":
            key = memory_key(value.get("statement", ""), value.get("input_columns"))
            users = {f.value.get("app_id") for f in await kg.facts(kind="memory_check", subject=f"memory:{key}")
                     if f.value.get("passed")}
            users |= {f.value.get("app_id") for f in await kg.facts(kind="translation_memory",
                                                                   subject=f"memory:{key}", current=False)
                      if f.value.get("status") != "disproved"}
            for app in sorted(a for a in users if a and a != value.get("app_id")):
                add(app, "a learned answer it used was disproved")
    return out


# why an application re-runs -> where the graph is entered (pipeline order: extract < resolve < translate)
ENTRY_FOR = {"variable value": "extract", "source mapped": "resolve"}
ORDER = ["extract", "resolve", "translate"]


def start_for(reasons: list[str]) -> tuple[str, bool]:
    """The earliest node the reasons need, and whether the translation can be reused (only source mappings)."""
    nodes = [next((v for k, v in ENTRY_FOR.items() if r.startswith(k)), "translate") for r in reasons]
    first = min(nodes, key=ORDER.index) if nodes else "translate"
    return first, all(n == "resolve" for n in nodes)


class EventConsumer:
    def __init__(self, deps: Any, cursor: Cursor | None = None, name: str = "orchestrator",
                 batch: int = 500) -> None:
        self.deps, self.cursor, self.name, self.batch = deps, cursor or MemoryCursor(), name, batch

    async def once(self) -> list[dict[str, Any]]:
        """Read the pending events, re-run the affected applications, then advance the position. A crash before
        the position is saved replays the batch: re-runs are idempotent."""
        after = self.cursor.get(self.name)
        events = await self.deps.kg.events(after, self.batch)
        if not events:
            return []
        apps = await affected(self.deps.kg, events)
        results = []
        if apps:
            log.info("events %s-%s: re-running %s", events[0]["seq"], events[-1]["seq"], sorted(apps))
            entries = {a: start_for(why) for a, why in apps.items()}
            hints = await hints_from_events(self.deps.kg, events, apps)
            scopes = {a: h.as_dict() for a, h in hints.items() if not h.everything}
            for r in await rerun_all(self.deps, apps=sorted(apps), run_prefix="event", start_at=entries,
                                     scopes=scopes):
                results.append(r | {"because": apps[r["app_id"]]})
        self.cursor.set(self.name, events[-1]["seq"])
        return results

    async def drain(self, max_batches: int = 20) -> list[dict[str, Any]]:
        """Handle events until none are pending (re-runs can raise further events, e.g. a disproved answer)."""
        out: list[dict[str, Any]] = []
        for _ in range(max_batches):
            before = self.cursor.get(self.name)
            out += await self.once()
            if self.cursor.get(self.name) == before:
                break
        return out

    async def run(self, interval: float = 15.0) -> None:
        while True:
            try:
                for r in await self.drain():
                    log.info("re-run %s", json.dumps(r, default=str))
            except Exception:  # noqa: BLE001 - keep following the outbox
                log.exception("event batch failed; retrying")
            await asyncio.sleep(interval)


async def _main(argv: list[str] | None = None) -> int:
    from adce_core import telemetry

    telemetry.setup("orchestrator-events")
    p = argparse.ArgumentParser(prog="orchestrator.events", description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", choices=["once", "run"])
    p.add_argument("--interval", type=float, default=15.0, help="seconds between polls (run)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from adce_core.artifacts import from_settings as artifacts_from_settings
    from adce_core.settings import get_settings
    from adce_gateway.gateway import from_settings as gateway_from_settings
    from adce_kg import KnowledgeService

    from .learning import agents_kg
    from .pipeline import Deps

    s = get_settings()
    kg = await KnowledgeService.from_settings(s)
    cursor = PostgresCursor(s.pg_dsn)
    try:
        async with agents_kg(s, kg) as akg:
            deps = Deps(kg=akg, artifacts=artifacts_from_settings(s), gateway=gateway_from_settings(s),
                        llm_tools=s.llm_tools)
            consumer = EventConsumer(deps, cursor)
            if args.command == "once":
                print(json.dumps(await consumer.drain(), indent=1, default=str))
            else:
                await consumer.run(args.interval)
        return 0
    finally:
        cursor.close()
        await kg.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
