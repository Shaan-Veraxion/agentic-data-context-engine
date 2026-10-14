"""The model gateway: the only route to AI models, whichever provider serves them.

Call path:
agent request -> privacy guard -> memo lookup -> budget check -> prompt assembly (cached system prefix)
-> provider (primary model, then fallbacks on provider errors) -> schema validation -> record + trace -> typed result.

Providers and models come from configuration (config.py); providers are clients in providers.py. Every provider
gets the same guarantees: no data rows leave (privacy guard), outputs are validated against the caller's schema,
responses are recorded so runs replay identically, and spending stops at the budget.

Modes:
- replay: answer only from recordings (tests, CI, demos); a missing recording is an error, never a live call
- record: call the provider and store the response as a recording
- live:   call the provider; responses are still memoized so packs regenerate identically
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import privacy
from .config import Binding, GatewayConfig, default_config, load_config
from .limits import RateLimiter, estimate_tokens
from .providers import GatewayRefusal, OutputInvalid, ProviderClient, ProviderError, Usage, build_client
from .routes import ROUTES, Route

__all__ = ["Budget", "BudgetExceeded", "GatewayRefusal", "GatewayResult", "ModelGateway", "OutputInvalid",
           "ProviderError", "Recordings", "ReplayMiss", "Usage", "from_settings"]


class ReplayMiss(LookupError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class GatewayResult:
    output: BaseModel
    route: str
    model: str  # model that answered, as the provider reports it
    prompt_version: str
    key: str
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    priced: bool = True  # False when the model has no configured price (cost reported as 0)
    source: str = "live"  # live | memo | replay
    latency_ms: int = 0
    attempts: list[str] = field(default_factory=list)  # provider:model tried, in order


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


def memo_key(route: Route, binding: str, system: str, user: str, output_model: type[BaseModel]) -> str:
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True)
    blob = "\x1f".join([route.name, binding, route.effort or "", route.prompt_version, system, user, schema])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


Tracer = Callable[[GatewayResult, dict[str, Any]], None]


class ModelGateway:
    def __init__(self, mode: str, recordings: Recordings, client: ProviderClient | None = None,
                 budget: Budget | None = None, routes: dict[str, Route] | None = None,
                 concurrency: int = 4, tracer: Tracer | None = None, *, config: GatewayConfig | None = None,
                 clients: dict[str, ProviderClient] | None = None) -> None:
        """client: one client used for every provider (tests); clients: per provider name. Clients not given are
        built from the configuration when first needed, so replay mode needs no keys."""
        if mode not in ("replay", "record", "live"):
            raise ValueError(f"unknown gateway mode {mode}")
        self.mode = mode
        self.recordings = recordings
        self.config = config or default_config()
        self._shared = client
        self.clients: dict[str, ProviderClient] = dict(clients or {})
        self.budget = budget or Budget(run_usd=float("inf"))
        self.routes = routes or ROUTES
        self._sem = asyncio.Semaphore(concurrency)
        self.tracer = tracer
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter(self, provider: str) -> RateLimiter | None:
        cfg = self.config.providers.get(provider)
        if cfg is None or (cfg.rpm is None and cfg.tpm is None):
            return None
        if provider not in self._limiters:
            self._limiters[provider] = RateLimiter(cfg.rpm, cfg.tpm)
        return self._limiters[provider]

    def _client(self, provider: str) -> ProviderClient:
        if self._shared is not None:
            return self._shared
        if provider not in self.clients:
            self.clients[provider] = build_client(self.config.providers[provider])
        return self.clients[provider]

    async def call(self, route_name: str, inputs: dict[str, Any], output_model: type[BaseModel], *,
                   task: str = "default") -> GatewayResult:
        route = self.routes[route_name]
        if not route.rows_allowed:
            privacy.check(inputs)
        override = self.config.routes.get(route.name)
        effort = (override.effort if override and override.effort else route.effort)
        max_tokens = (override.max_tokens if override and override.max_tokens else route.max_tokens)
        bindings = self.config.bindings(route.name, route.tier)
        system = route.system_prompt()
        user = json.dumps(inputs, sort_keys=True, default=str)
        key = memo_key(route, bindings[0].key, system, user, output_model)

        rec = self.recordings.get(route.name, key)
        if rec is not None:
            out = self._validate(output_model, rec["output"], route)
            return GatewayResult(output=out, route=route.name, model=rec["model"], provider=rec.get("provider", ""),
                                 prompt_version=route.prompt_version, key=key,
                                 source="replay" if self.mode == "replay" else "memo")
        if self.mode == "replay":
            raise ReplayMiss(f"no recording for route {route.name} key {key}")

        self.budget.check(task)
        t0 = time.perf_counter()
        attempts: list[str] = []
        errors: list[str] = []
        for b in bindings:
            attempts.append(b.key)
            cap = self.config.providers[b.provider].max_output_tokens if b.provider in self.config.providers else None
            out_tokens = min(max_tokens, cap) if cap else max_tokens
            limiter = self._limiter(b.provider)
            # the request as the provider counts it: prompt plus output budget; corrected with reported usage after
            entry = await limiter.acquire(estimate_tokens(system + user) + out_tokens) if limiter else None
            try:
                async with self._sem:
                    out, usage, model = await self._complete_with_retry(b, system, user, output_model, out_tokens,
                                                                        effort)
                if entry is not None:
                    RateLimiter.settle(entry, usage.input_tokens + usage.cache_read_tokens + usage.output_tokens)
            except ProviderError as e:
                errors.append(str(e))
                continue  # next fallback
            return self._finish(route, b, key, inputs, out, usage, model, task, t0, attempts)
        raise ProviderError(f"route {route.name}: every provider failed: {' | '.join(errors)}")

    def _finish(self, route: Route, b: Binding, key: str, inputs: dict[str, Any], out: BaseModel, usage: Usage,
                model: str, task: str, t0: float, attempts: list[str]) -> GatewayResult:
        price = self.config.price(b)
        cost = ((usage.input_tokens * price.input + usage.output_tokens * price.output
                 + usage.cache_read_tokens * price.cache_read) / 1e6) if price else 0.0
        self.budget.add(task, cost)
        result = GatewayResult(output=out, route=route.name, model=model, provider=b.provider,
                               prompt_version=route.prompt_version, key=key, usage=usage, cost_usd=cost,
                               priced=price is not None, source="live",
                               latency_ms=int((time.perf_counter() - t0) * 1000), attempts=attempts)
        self.recordings.put(route.name, key, {"route": route.name, "provider": b.provider, "model": model,
                                              "binding": b.key, "prompt_version": route.prompt_version,
                                              "inputs": inputs, "output": out.model_dump(mode="json"),
                                              "usage": usage.model_dump(), "cost_usd": cost})
        if self.tracer is not None:
            self.tracer(result, inputs)
        return result

    async def _complete_with_retry(self, b: Binding, system: str, user: str, output_model: type[BaseModel],
                                   max_tokens: int, effort: str | None) -> tuple[BaseModel, Usage, str]:
        client = self._client(b.provider)
        try:
            return await client.complete(b.model, system, user, output_model, max_tokens=max_tokens, effort=effort)
        except OutputInvalid:
            # one retry; after that the caller sends the item to the review queue
            return await client.complete(b.model, system, user, output_model, max_tokens=max_tokens, effort=effort)

    @staticmethod
    def _validate(output_model: type[BaseModel], data: Any, route: Route) -> BaseModel:
        try:
            return output_model.model_validate(data)
        except ValidationError as e:
            raise OutputInvalid(f"recording for {route.name} does not match {output_model.__name__}: {e}") from e


def from_settings(settings: Any, budget: Budget | None = None, tracer: Tracer | None = None) -> ModelGateway:
    """ADCE_GATEWAY_CONFIG names a gateway.toml; without it, Claude on Vertex AI (ADCE_VERTEX_PROJECT)."""
    path = getattr(settings, "gateway_config", None)
    config = load_config(path) if path else default_config(getattr(settings, "vertex_region", "global"))
    if settings.gateway_mode != "replay" and not path and not settings.vertex_project:
        raise ValueError("outside replay mode set ADCE_GATEWAY_CONFIG, or ADCE_VERTEX_PROJECT for Claude on Vertex")
    if tracer is None:
        from .tracing import langfuse_tracer

        tracer = langfuse_tracer()
    return ModelGateway(settings.gateway_mode, Recordings(settings.gateway_replay_dir), budget=budget,
                        config=config, tracer=tracer)
