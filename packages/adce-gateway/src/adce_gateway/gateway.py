"""The model gateway: the only route to AI models.

Call path (architecture-and-workflow proposal, Figure 7):
agent request -> privacy guard -> memo lookup -> budget check -> prompt assembly (cached system prefix)
-> Vertex AI (Claude, structured output) -> schema validation -> record + trace -> typed result.

Modes:
- replay: answer only from recordings (tests, CI, demos); a missing recording is an error, never a live call
- record: call Vertex and store the response as a recording
- live:   call Vertex; responses are still memoized so packs regenerate identically
Not on Vertex AI: Message Batches, server-side fallbacks, task budgets. Bulk work runs as rate-limited concurrent
calls, refusals are handled here, and budgets are enforced here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from . import privacy
from .routes import PRICES, ROUTES, Route


class ReplayMiss(LookupError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class GatewayRefusal(RuntimeError):
    pass


class OutputInvalid(ValueError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost(self, model: str) -> float:
        pin, pout, pcache = PRICES.get(model, (0.0, 0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout + self.cache_read_tokens * pcache) / 1e6


@dataclass
class GatewayResult:
    output: BaseModel
    route: str
    model: str
    prompt_version: str
    key: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    source: str = "live"  # live | memo | replay
    latency_ms: int = 0


class Budget:
    def __init__(self, run_usd: float, task_usd: float | None = None) -> None:
        self.run_usd = run_usd
        self.task_usd = task_usd
        self.spent_run = 0.0
        self.spent_task: dict[str, float] = {}

    def check(self, task: str) -> None:
        if self.spent_run >= self.run_usd:
            raise BudgetExceeded(f"run budget {self.run_usd} USD reached")
        if self.task_usd is not None and self.spent_task.get(task, 0.0) >= self.task_usd:
            raise BudgetExceeded(f"task budget {self.task_usd} USD reached for {task}")

    def add(self, task: str, usd: float) -> None:
        self.spent_run += usd
        self.spent_task[task] = self.spent_task.get(task, 0.0) + usd


class Recordings:
    """Content-addressed store of responses, used both as the memo cache and as replay fixtures."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, route: str, key: str) -> Path:
        return self.root / route / f"{key}.json"

    def get(self, route: str, key: str) -> dict[str, Any] | None:
        p = self._path(route, key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, route: str, key: str, record: dict[str, Any]) -> None:
        p = self._path(route, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")


class ModelClient(Protocol):
    async def complete(self, route: Route, system: str, user: str,
                       output_model: type[BaseModel]) -> tuple[BaseModel, Usage, str]: ...


class VertexClaude:
    """Claude on Google Cloud Vertex AI through the Anthropic SDK (anthropic[vertex])."""

    def __init__(self, project_id: str, region: str) -> None:
        from anthropic.lib.vertex import AsyncAnthropicVertex

        self.client = AsyncAnthropicVertex(project_id=project_id, region=region)

    async def complete(self, route: Route, system: str, user: str,
                       output_model: type[BaseModel]) -> tuple[BaseModel, Usage, str]:
        kwargs: dict[str, Any] = {}
        if route.effort:
            kwargs["output_config"] = {"effort": route.effort}
        resp = await self.client.messages.parse(
            model=route.model, max_tokens=route.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=output_model, **kwargs)
        if resp.stop_reason == "refusal":
            raise GatewayRefusal(f"route {route.name}: model refused")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise OutputInvalid(f"route {route.name}: no complete structured output (stop={resp.stop_reason})")
        u = resp.usage
        usage = Usage(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        return resp.parsed_output, usage, resp.model


def memo_key(route: Route, system: str, user: str, output_model: type[BaseModel]) -> str:
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True)
    blob = "\x1f".join([route.name, route.model, route.effort or "", route.prompt_version, system, user, schema])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class ModelGateway:
    def __init__(self, mode: str, recordings: Recordings, client: ModelClient | None = None,
                 budget: Budget | None = None, routes: dict[str, Route] | None = None,
                 concurrency: int = 4, tracer: Any | None = None) -> None:
        if mode not in ("replay", "record", "live"):
            raise ValueError(f"unknown gateway mode {mode}")
        if mode != "replay" and client is None:
            raise ValueError(f"mode {mode} needs a model client")
        self.mode = mode
        self.recordings = recordings
        self.client = client
        self.budget = budget or Budget(run_usd=float("inf"))
        self.routes = routes or ROUTES
        self._sem = asyncio.Semaphore(concurrency)
        self.tracer = tracer

    async def call(self, route_name: str, inputs: dict[str, Any], output_model: type[BaseModel], *,
                   task: str = "default") -> GatewayResult:
        route = self.routes[route_name]
        if not route.rows_allowed:
            privacy.check(inputs)
        system = route.system_prompt()
        user = json.dumps(inputs, sort_keys=True, default=str)
        key = memo_key(route, system, user, output_model)

        rec = self.recordings.get(route.name, key)
        if rec is not None:
            out = self._validate(output_model, rec["output"], route)
            return GatewayResult(output=out, route=route.name, model=rec["model"], prompt_version=route.prompt_version,
                                 key=key, source="replay" if self.mode == "replay" else "memo")
        if self.mode == "replay":
            raise ReplayMiss(f"no recording for route {route.name} key {key}")

        self.budget.check(task)
        assert self.client is not None
        t0 = time.perf_counter()
        async with self._sem:
            out, usage, model = await self._complete_with_retry(route, system, user, output_model)
        cost = usage.cost(route.model)
        self.budget.add(task, cost)
        result = GatewayResult(output=out, route=route.name, model=model, prompt_version=route.prompt_version, key=key,
                               usage=usage, cost_usd=cost, source="live",
                               latency_ms=int((time.perf_counter() - t0) * 1000))
        self.recordings.put(route.name, key, {"route": route.name, "model": model,
                                              "prompt_version": route.prompt_version, "inputs": inputs,
                                              "output": out.model_dump(mode="json"), "usage": usage.__dict__,
                                              "cost_usd": cost})
        if self.tracer is not None:
            self.tracer(result, inputs)
        return result

    async def _complete_with_retry(self, route: Route, system: str, user: str,
                                   output_model: type[BaseModel]) -> tuple[BaseModel, Usage, str]:
        assert self.client is not None
        try:
            return await self.client.complete(route, system, user, output_model)
        except OutputInvalid:
            # one retry; after that the caller sends the item to the review queue
            return await self.client.complete(route, system, user, output_model)

    @staticmethod
    def _validate(output_model: type[BaseModel], data: Any, route: Route) -> BaseModel:
        try:
            return output_model.model_validate(data)
        except ValidationError as e:
            raise OutputInvalid(f"recording for {route.name} does not match {output_model.__name__}: {e}") from e


def from_settings(settings: Any, budget: Budget | None = None) -> ModelGateway:
    client = None
    if settings.gateway_mode != "replay":
        if not settings.vertex_project:
            raise ValueError("ADCE_VERTEX_PROJECT is required outside replay mode")
        client = VertexClaude(settings.vertex_project, settings.vertex_region)
    return ModelGateway(settings.gateway_mode, Recordings(settings.gateway_replay_dir), client, budget)
