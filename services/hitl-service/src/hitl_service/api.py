"""HTTP API for review items. Mounted by the API gateway (/review) and runnable on its own as hitl-service.

Run standalone:  uv run uvicorn hitl_service.api:app --port 8702
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .service import DecisionError, ReviewItem, ReviewService


class DecisionIn(BaseModel):
    decision: str
    payload: dict[str, Any] = {}


class CorrectionIn(BaseModel):
    fact_id: str
    correction: dict[str, Any]
    reason: str = ""


class StatementSqlIn(BaseModel):
    table: str
    statement: int
    span: str
    sql: str
    text: str = ""
    input_columns: list[str] | None = None


class SourcesIn(BaseModel):
    mappings: dict[str, str]  # source subject -> Dremio dataset


def item_out(i: ReviewItem) -> dict[str, Any]:
    return i.model_dump(mode="json") | {"score": round(i.score, 3)}


def create_router(get_service: Callable[..., ReviewService], reviewer: Callable[..., str]) -> APIRouter:
    r = APIRouter()

    @r.post("/apps/{app_id}/refresh")
    async def refresh(app_id: str, svc: ReviewService = Depends(get_service)) -> dict[str, int]:
        return {"new_items": await svc.refresh(app_id)}

    @r.get("/items")
    async def items(app_id: str | None = None, status: str | None = "open",
                    svc: ReviewService = Depends(get_service)) -> list[dict[str, Any]]:
        return [item_out(i) for i in svc.queue(app_id, status)]

    @r.post("/items/{item_id}/decision")
    async def decide(item_id: str, body: DecisionIn, svc: ReviewService = Depends(get_service),
                     who: str = Depends(reviewer)) -> dict[str, Any]:
        try:
            return item_out(await svc.decide(item_id, body.decision, who, body.payload))
        except DecisionError as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/items/{item_id}/reopen")
    async def reopen(item_id: str, svc: ReviewService = Depends(get_service),
                     who: str = Depends(reviewer)) -> dict[str, Any]:
        """Undo a decision (also the auto-reviewer's): its fact is invalidated and the item is open again."""
        try:
            return item_out(await svc.reopen(item_id, who))
        except DecisionError as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/correct")
    async def correct(body: CorrectionIn, svc: ReviewService = Depends(get_service),
                      who: str = Depends(reviewer)) -> dict[str, str]:
        """Correct mode: edit any fact's value; the edit is a confirmed fact superseding it."""
        try:
            return {"fact_id": await svc.correct(body.fact_id, body.correction, who, body.reason)}
        except (DecisionError, KeyError) as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/apps/{app_id}/statement_sql")
    async def statement_sql(app_id: str, body: StatementSqlIn, svc: ReviewService = Depends(get_service),
                            who: str = Depends(reviewer)) -> dict[str, str]:
        """Correct the SQL of one statement (translated by the rules or not); the next run uses it."""
        try:
            return {"fact_id": await svc.statement_sql(app_id, body.table, body.statement, body.span, body.sql, who,
                                                       body.text, body.input_columns)}
        except DecisionError as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/apps/{app_id}/sources")
    async def map_sources(app_id: str, body: SourcesIn, svc: ReviewService = Depends(get_service),
                          who: str = Depends(reviewer)) -> dict[str, Any]:
        """Map several sources to their Dremio datasets at once."""
        done = {}
        for subject, dataset in body.mappings.items():
            if dataset.strip():
                try:
                    done[subject] = await svc.map_source(subject, dataset, who)
                except DecisionError as e:
                    raise HTTPException(409, f"{subject}: {e}") from e
        await svc.refresh(app_id)
        return {"mapped": done}

    @r.get("/apps/{app_id}/auto")
    async def auto(app_id: str, svc: ReviewService = Depends(get_service)) -> dict[str, Any]:
        """What the auto-reviewer did: automatic acceptances (and how many reviewers reversed), and the items it
        decided (reopen any of them)."""
        from .auto_review import AUTO_REVIEWER, stats

        decided = [item_out(i) for i in svc.queue(app_id, "decided") if i.decided_by == AUTO_REVIEWER]
        accepted = [f.model_dump(mode="json") | {"id": f.id} for f in await svc.kg.facts(kind="auto_accepted")
                    if f.value.get("app_id") == app_id]
        return {"stats": await stats(svc.kg), "accepted": accepted, "decided_items": decided}

    return r


def _service(request: Request) -> ReviewService:
    return request.app.state.review


def _reviewer(request: Request) -> str:
    return request.headers.get("x-reviewer", "local-dev")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from adce_core.settings import get_settings
    from adce_gateway.gateway import from_settings as gateway_from_settings
    from adce_kg import KnowledgeService

    from .service import PostgresReviewStore

    s = get_settings()
    kg = await KnowledgeService.from_settings(s)
    store = PostgresReviewStore(s.pg_dsn)
    app.state.review = ReviewService(store, kg, gateway_from_settings(s))
    try:
        yield
    finally:
        store.close()
        await kg.close()


app = FastAPI(title="ADCE hitl-service", version="0.1.0", lifespan=_lifespan)
app.include_router(create_router(_service, _reviewer))
