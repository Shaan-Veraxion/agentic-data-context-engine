"""Gateway configuration: which provider and model answers each route.

Routes ask for a tier (strong / standard / light), never for a model. One configuration file maps tiers to
"provider:model" and declares the providers, so the same build ships to installations with different model
providers and keys. A route can override its model and list fallbacks, tried in order when a provider fails.

    # gateway.toml  (ADCE_GATEWAY_CONFIG=/path/to/gateway.toml)
    [providers.groq]
    kind = "openai_compatible"
    base_url = "https://api.groq.com/openai/v1"
    api_key_env = "GROQ_API_KEY"
    structured_output = "json_object"

    [models]
    strong = "groq:<model id>"
    standard = "groq:<model id>"
    light = "groq:<model id>"

    [routes.repair_sql]
    model = "strong"
    fallbacks = ["standard"]

    [prices."groq:<model id>"]       # USD per million tokens; unpriced models cost 0 and are reported as unpriced
    input = 0.15
    output = 0.6

Without a file the gateway uses Claude on Vertex AI (ADCE_VERTEX_PROJECT, ADCE_VERTEX_REGION), as before.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .providers import ProviderConfig

TIERS = ("strong", "standard", "light")
CLAUDE = {"strong": "claude-opus-5-5", "standard": "claude-sonnet-5-5", "light": "claude-haiku-4-5"}
# USD per million tokens (input, output, cache read) for the Claude models; confirm per platform and region (Q-25).
CLAUDE_PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
                 "claude-haiku-4-5": (1.0, 5.0, 0.10)}


class Price(BaseModel):
    input: float
    output: float
    cache_read: float = 0.0


class RouteOverride(BaseModel):
    model: str | None = None  # a tier, an alias from [models], or "provider:model"
    fallbacks: list[str] = Field(default_factory=list)
    effort: str | None = None
    max_tokens: int | None = None


class Binding(BaseModel):
    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)  # tier or alias -> "provider:model" (or another alias)
    routes: dict[str, RouteOverride] = Field(default_factory=dict)
    prices: dict[str, Price] = Field(default_factory=dict)  # "provider:model" or a bare model id

    def resolve(self, ref: str) -> Binding:
        seen: list[str] = []
        while ref in self.models:
            if ref in seen:
                raise ValueError(f"model alias loop: {' -> '.join([*seen, ref])}")
            seen.append(ref)
            ref = self.models[ref]
        provider, sep, model = ref.partition(":")
        if not sep or provider not in self.providers:
            raise ValueError(f"model {ref!r}: expected 'provider:model' with a provider from [providers] "
                             f"({', '.join(self.providers) or 'none configured'})")
        return Binding(provider=provider, model=model)

    def bindings(self, route_name: str, tier: str) -> list[Binding]:
        o = self.routes.get(route_name, RouteOverride())
        refs = [o.model or tier, *o.fallbacks]
        out: list[Binding] = []
        for r in refs:
            b = self.resolve(r)
            if all(b.key != x.key for x in out):
                out.append(b)
        return out

    def price(self, b: Binding) -> Price | None:
        if p := self.prices.get(b.key) or self.prices.get(b.model):
            return p
        if b.model in CLAUDE_PRICES and self.providers[b.provider].kind.startswith("anthropic"):
            i, o, c = CLAUDE_PRICES[b.model]
            return Price(input=i, output=o, cache_read=c)
        return None


def load_config(path: str | Path) -> GatewayConfig:
    data: dict[str, Any] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for name, p in data.get("providers", {}).items():
        p.setdefault("name", name)
    cfg = GatewayConfig.model_validate(data)
    for tier in TIERS:  # every route needs its tier
        if tier not in cfg.models:
            raise ValueError(f"{path}: [models] must define '{tier}'")
        cfg.resolve(tier)
    return cfg


def default_config(vertex_region: str = "global") -> GatewayConfig:
    """Claude on Vertex AI for every tier: the configuration used when no file is given."""
    return GatewayConfig(
        providers={"vertex": ProviderConfig(name="vertex", kind="anthropic_vertex", project_env="ADCE_VERTEX_PROJECT",
                                            region=vertex_region)},
        models={tier: f"vertex:{model}" for tier, model in CLAUDE.items()})
