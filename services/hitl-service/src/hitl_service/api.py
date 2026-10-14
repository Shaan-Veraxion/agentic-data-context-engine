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

    return r


def _service(request: Request) -> ReviewService:
    return request.app.state.review


def _reviewer(request: Request) -> str:
    return request.headers.get("x-reviewer", "local-dev")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from adce_core.settings import get_settings
    from adce_kg import KnowledgeService

    from .service import PostgresReviewStore

    s = get_settings()
    kg = await KnowledgeService.from_settings(s)
    store = PostgresReviewStore(s.pg_dsn)
    app.state.review = ReviewService(store, kg)
    try:
        yield
    finally:
        store.close()
        await kg.close()


app = FastAPI(title="ADCE hitl-service", version="0.1.0", lifespan=_lifespan)
app.include_router(create_router(_service, _reviewer))
