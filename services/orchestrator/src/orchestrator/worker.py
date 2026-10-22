"""The orchestrator as its own worker: runs are queued (by the API gateway) and executed here, not inside the API.

A request is either a new run (kind run: app, run id, files) or the continuation of a paused run (kind resume:
the owner's answers). The worker claims one queued request at a time (Postgres: FOR UPDATE SKIP LOCKED, so several
workers can share the queue), runs or resumes the LangGraph pipeline with the Postgres checkpointer, refreshes the
review queue, and stores the outcome on the request. A crash leaves the request "running"; the checkpointer keeps
the graph's progress, and a request older than `stale_after` is claimed again and resumed from its checkpoint.

    uv run python -m orchestrator.worker            # follow the queue (Kubernetes: orchestrator-worker)
    uv run python -m orchestrator.worker --once     # run what is queued, then stop
The API queues instead of running inline when ADCE_ORCHESTRATOR_MODE=worker.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .pipeline import Deps, resume_pipeline, run_pipeline

log = logging.getLogger("adce.worker")


@dataclass
class RunRequest:
    id: int
    kind: str  # run | resume
    app_id: str
    run_id: str
    payload: dict[str, Any]  # run: {files, app_name}; resume: {answer}
    status: str = "queued"  # queued | running | done | waiting_for_owner | error
    result: dict[str, Any] = field(default_factory=dict)


class RunQueue(Protocol):
    def put(self, kind: str, app_id: str, run_id: str, payload: dict[str, Any]) -> int: ...

    def claim(self) -> RunRequest | None: ...

    def finish(self, req: RunRequest, status: str, result: dict[str, Any]) -> None: ...

    def get(self, request_id: int) -> RunRequest | None: ...

    def latest(self, app_id: str, run_id: str) -> RunRequest | None: ...


class MemoryRunQueue:
    def __init__(self) -> None:
        self.items: list[RunRequest] = []

    def put(self, kind: str, app_id: str, run_id: str, payload: dict[str, Any]) -> int:
        self.items.append(RunRequest(len(self.items) + 1, kind, app_id, run_id, payload))
        return self.items[-1].id

    def claim(self) -> RunRequest | None:
        for r in self.items:
            if r.status == "queued":
                r.status = "running"
                return r
        return None

    def finish(self, req: RunRequest, status: str, result: dict[str, Any]) -> None:
        req.status, req.result = status, result

    def get(self, request_id: int) -> RunRequest | None:
        return next((r for r in self.items if r.id == request_id), None)

    def latest(self, app_id: str, run_id: str) -> RunRequest | None:
        mine = [r for r in self.items if r.app_id == app_id and r.run_id == run_id]
        return mine[-1] if mine else None


class PostgresRunQueue:
    """Table run_request (migration 0004)."""

    COLS = "id, kind, app_id, run_id, payload, status, result"

    def __init__(self, dsn: str, stale_after: dt.timedelta = dt.timedelta(minutes=30)) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True, kwargs={"row_factory": dict_row})
        self.stale_after = stale_after

    def close(self) -> None:
        self.pool.close()

    def _row(self, r: dict[str, Any] | None) -> RunRequest | None:
        return RunRequest(**r) if r else None

    def put(self, kind: str, app_id: str, run_id: str, payload: dict[str, Any]) -> int:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as con:
            row = con.execute("INSERT INTO run_request (kind, app_id, run_id, payload) VALUES (%s,%s,%s,%s) "
                              "RETURNING id", (kind, app_id, run_id, Jsonb(payload))).fetchone()
        assert row is not None
        return int(row["id"])

    def claim(self) -> RunRequest | None:
        with self.pool.connection() as con:
            row = con.execute(
                "UPDATE run_request SET status = 'running', started_at = now() WHERE id = ("
                " SELECT id FROM run_request WHERE status = 'queued'"
                "   OR (status = 'running' AND started_at < now() - %s)"
                " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                f"RETURNING {self.COLS}", (self.stale_after,)).fetchone()
        return self._row(row)

    def finish(self, req: RunRequest, status: str, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as con:
            con.execute("UPDATE run_request SET status = %s, result = %s, finished_at = now() WHERE id = %s",
                        (status, Jsonb(result), req.id))

    def get(self, request_id: int) -> RunRequest | None:
        with self.pool.connection() as con:
            return self._row(con.execute(f"SELECT {self.COLS} FROM run_request WHERE id = %s",
                                         (request_id,)).fetchone())

    def latest(self, app_id: str, run_id: str) -> RunRequest | None:
        with self.pool.connection() as con:
            return self._row(con.execute(f"SELECT {self.COLS} FROM run_request WHERE app_id = %s AND run_id = %s "
                                         "ORDER BY id DESC LIMIT 1", (app_id, run_id)).fetchone())


def outcome(out: Any) -> tuple[str, dict[str, Any]]:
    """The request's status and result from the pipeline's final (or paused) state."""
    paused = out.get("__interrupt__")
    result = {k: out.get(k) for k in ("status", "tables", "levels", "pending", "pack_prefix", "decisions")}
    if paused:
        result["questions"] = [q for i in paused for q in (getattr(i, "value", {}) or {}).get("questions", [])]
        return "waiting_for_owner", result
    return "done", result


class Worker:
    def __init__(self, queue: RunQueue, deps: Deps, checkpointer: Any | None = None, review: Any | None = None) -> None:
        self.queue, self.deps, self.checkpointer, self.review = queue, deps, checkpointer, review

    async def once(self) -> RunRequest | None:
        req = self.queue.claim()
        if req is None:
            return None
        try:
            if req.kind == "resume":
                out = await resume_pipeline(app_id=req.app_id, run_id=req.run_id, deps=self.deps,
                                            checkpointer=self.checkpointer, answer=req.payload.get("answer", {}))
            else:
                out = await run_pipeline(app_id=req.app_id, run_id=req.run_id, files=req.payload["files"],
                                         app_name=req.payload.get("app_name"), deps=self.deps,
                                         checkpointer=self.checkpointer)
            status, result = outcome(out)
            if self.review is not None:
                result["new_review_items"] = await self.review.refresh(req.app_id)
        except Exception as e:  # noqa: BLE001 - recorded on the request; the worker carries on
            log.exception("run %s failed", req.run_id)
            status, result = "error", {"error": f"{type(e).__name__}: {e}"}
        self.queue.finish(req, status, result)
        return req

    async def drain(self) -> list[RunRequest]:
        done = []
        while (r := await self.once()) is not None:
            done.append(r)
        return done

    async def run(self, interval: float = 2.0) -> None:
        while True:
            if await self.once() is None:
                await asyncio.sleep(interval)


async def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.worker", description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--once", action="store_true", help="run what is queued, then stop")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    from adce_core.artifacts import from_settings as artifacts_from_settings
    from adce_core.settings import get_settings
    from adce_gateway.gateway import from_settings as gateway_from_settings
    from adce_kg import KnowledgeService
    from hitl_service.service import PostgresReviewStore, ReviewService
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from .learning import agents_kg

    s = get_settings()
    kg = await KnowledgeService.from_settings(s)
    queue = PostgresRunQueue(s.pg_dsn)
    store = PostgresReviewStore(s.pg_dsn)
    try:
        async with AsyncPostgresSaver.from_conn_string(s.pg_dsn) as saver, agents_kg(s, kg) as akg:
            await saver.setup()
            deps = Deps(kg=akg, artifacts=artifacts_from_settings(s), gateway=gateway_from_settings(s),
                        llm_tools=s.llm_tools, supervisor=s.supervisor, interactive=s.interactive_runs,
                        budget_usd=s.run_budget_usd, max_calls=s.run_max_calls, tolerance=s.validation_tolerance)
            worker = Worker(queue, deps, saver, ReviewService(store, kg, deps.gateway))
            if args.once:
                print(json.dumps([{"id": r.id, "status": r.status} for r in await worker.drain()]))
            else:
                await worker.run()
        return 0
    finally:
        queue.close()
        store.close()
        await kg.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
