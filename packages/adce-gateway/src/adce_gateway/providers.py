"""Model providers: one client per kind of API. The gateway talks to every provider through `ProviderClient`.

Kinds:
- anthropic          Claude through the Anthropic API (API key)
- anthropic_vertex   Claude on Google Cloud Vertex AI (Google credentials; project + region)
- anthropic_bedrock  Claude on Amazon Bedrock (AWS credentials; region)
- openai_compatible  any OpenAI-compatible chat-completions API: OpenAI, Azure OpenAI, Groq, Gemini (OpenAI endpoint),
                     Together, Mistral, a local vLLM or Ollama, ...

Secrets never live in configuration: a provider names the environment variable that holds its key, so each
installation supplies its own keys (Kubernetes secrets, Secret Manager, a local .env).
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

KINDS = ("anthropic", "anthropic_vertex", "anthropic_bedrock", "openai_compatible")


class ProviderError(RuntimeError):
    """The provider could not answer (network, rate limit after retries, server error). Fallbacks may be tried."""


class GatewayRefusal(RuntimeError):
    pass


class OutputInvalid(ValueError):
    pass


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ProviderConfig(BaseModel):
    name: str
    kind: Literal["anthropic", "anthropic_vertex", "anthropic_bedrock", "openai_compatible"]
    api_key_env: str | None = None  # environment variable holding the API key
    base_url: str | None = None  # openai_compatible: the API root (…/v1); anthropic: optional proxy
    base_url_env: str | None = None  # or read the base URL from this environment variable
    project_env: str | None = None  # anthropic_vertex: environment variable with the Google Cloud project
    project: str | None = None
    region: str | None = None  # anthropic_vertex / anthropic_bedrock
    # openai_compatible: how structured output is requested. json_schema: the API enforces the schema;
    # json_object: JSON mode, schema given in the prompt and validated here; none: prompt only, validated here.
    structured_output: Literal["json_schema", "json_object", "none"] = "json_schema"
    reasoning_effort: bool = False  # openai_compatible: send `reasoning_effort` (reasoning models only)
    max_tokens_param: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"  # older servers
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 600.0
    max_retries: int = 2

    def env(self, var: str | None) -> str | None:
        return os.environ.get(var) if var else None


class ProviderClient(Protocol):
    async def complete(self, model: str, system: str, user: str, output_model: type[BaseModel], *,
                       max_tokens: int, effort: str | None) -> tuple[BaseModel, Usage, str]: ...


# ---------------------------------------------------------------------------------------------- Anthropic (Claude)
class AnthropicClient:
    """Claude through the Anthropic SDK: direct API, Vertex AI or Bedrock. Structured output via messages.parse."""

    def __init__(self, cfg: ProviderConfig) -> None:
        import anthropic

        self.cfg = cfg
        common: dict[str, Any] = {"max_retries": cfg.max_retries, "timeout": cfg.timeout_s}
        if cfg.headers:
            common["default_headers"] = cfg.headers
        if cfg.kind == "anthropic":
            key = cfg.env(cfg.api_key_env or "ANTHROPIC_API_KEY")
            if not key:
                raise ValueError(f"provider {cfg.name}: set {cfg.api_key_env or 'ANTHROPIC_API_KEY'}")
            base = cfg.base_url or cfg.env(cfg.base_url_env)
            self.client: Any = anthropic.AsyncAnthropic(api_key=key, base_url=base, **common)
        elif cfg.kind == "anthropic_vertex":
            from anthropic.lib.vertex import AsyncAnthropicVertex

            project = cfg.project or cfg.env(cfg.project_env or "ADCE_VERTEX_PROJECT")
            if not project:
                raise ValueError(f"provider {cfg.name}: set {cfg.project_env or 'ADCE_VERTEX_PROJECT'}")
            self.client = AsyncAnthropicVertex(project_id=project, region=cfg.region or "global", **common)
        else:
            from anthropic.lib.bedrock import AsyncAnthropicBedrock

            self.client = AsyncAnthropicBedrock(aws_region=cfg.region, **common)
        self._errors = (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)

    async def complete(self, model: str, system: str, user: str, output_model: type[BaseModel], *,
                       max_tokens: int, effort: str | None) -> tuple[BaseModel, Usage, str]:
        kwargs: dict[str, Any] = {}
        if effort:
            kwargs["output_config"] = {"effort": effort}
        try:
            resp = await self.client.messages.parse(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}], output_format=output_model, **kwargs)
        except self._errors as e:
            raise ProviderError(f"{self.cfg.name}: {type(e).__name__}: {e}") from e
        if resp.stop_reason == "refusal":
            raise GatewayRefusal(f"{self.cfg.name}/{model}: model refused")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise OutputInvalid(f"{self.cfg.name}/{model}: no complete structured output (stop={resp.stop_reason})")
        u = resp.usage
        usage = Usage(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        return resp.parsed_output, usage, resp.model


# ---------------------------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatibleClient:
    """Any OpenAI-compatible chat-completions API. The output is always validated against the Pydantic model here,
    whatever the API promises, so a provider that ignores the schema is caught and retried once by the gateway."""

    def __init__(self, cfg: ProviderConfig) -> None:
        import openai

        self.cfg = cfg
        base = cfg.base_url or cfg.env(cfg.base_url_env)
        key = cfg.env(cfg.api_key_env) if cfg.api_key_env else None
        if cfg.api_key_env and not key:
            raise ValueError(f"provider {cfg.name}: set {cfg.api_key_env}")
        self.client = openai.AsyncOpenAI(api_key=key or "not-needed", base_url=base, max_retries=cfg.max_retries,
                                         timeout=cfg.timeout_s, default_headers=cfg.headers or None)
        self._errors = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)

    async def complete(self, model: str, system: str, user: str, output_model: type[BaseModel], *,
                       max_tokens: int, effort: str | None) -> tuple[BaseModel, Usage, str]:
        schema = output_model.model_json_schema()
        kwargs: dict[str, Any] = {}
        if self.cfg.structured_output == "json_schema":
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": output_model.__name__, "schema": _strict(schema), "strict": True}}
        elif self.cfg.structured_output == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if self.cfg.structured_output != "json_schema":
            system = (f"{system}\n\nAnswer with one JSON object only, matching this JSON Schema:\n"
                      f"{json.dumps(schema, sort_keys=True)}")
        if effort and self.cfg.reasoning_effort:
            kwargs["reasoning_effort"] = {"xhigh": "high", "max": "high"}.get(effort, effort)
        try:
            kwargs[self.cfg.max_tokens_param] = max_tokens
            messages: list[Any] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            resp = await self.client.chat.completions.create(model=model, messages=messages, **kwargs)
        except self._errors as e:
            raise ProviderError(f"{self.cfg.name}: {type(e).__name__}: {e}") from e
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise GatewayRefusal(f"{self.cfg.name}/{model}: model refused")
        if choice.finish_reason == "length":
            raise OutputInvalid(f"{self.cfg.name}/{model}: output truncated at max_tokens")
        text = _strip_fences(choice.message.content or "")
        try:
            out = output_model.model_validate_json(text)
        except ValidationError as e:
            raise OutputInvalid(f"{self.cfg.name}/{model}: output does not match {output_model.__name__}") from e
        u = resp.usage
        usage = Usage()
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            usage = Usage(input_tokens=u.prompt_tokens - cached, output_tokens=u.completion_tokens,
                          cache_read_tokens=cached)
        return out, usage, resp.model or model


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI strict mode: every object lists all its properties as required and allows no others."""
    s = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(s)
    return s


def build_client(cfg: ProviderConfig) -> ProviderClient:
    if cfg.kind == "openai_compatible":
        return OpenAICompatibleClient(cfg)
    return AnthropicClient(cfg)
