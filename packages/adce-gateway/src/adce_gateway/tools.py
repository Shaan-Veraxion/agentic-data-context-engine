"""Tools a model may call during a route, whatever the provider.

An LLM step can be given read-only tools (kg-mcp lookups, a SQL check). The gateway runs the conversation: the model
calls tools, the gateway runs them and returns their results, and the model finishes by calling `submit_answer`,
whose arguments are the route's structured output. This works the same on every provider that supports tool calls
(Claude, gpt-oss, Gemini, ...) and does not depend on a provider combining tools with a JSON-schema mode.

Tool results pass the privacy guard like any prompt input: a result that looks like rows is withheld.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from . import privacy

SUBMIT = "submit_answer"
# appended to the route's system prompt when tools are offered (the prompt itself stays the same for both modes)
TOOLS_NOTE = ("\n\n## Tools\nYou can call the tools offered before answering: use a check tool to test a candidate "
              "against the real checks and data, and the lookup tools when the input does not say enough. Do not "
              "call the same tool with the same arguments twice. Finish by calling submit_answer; its arguments are "
              "your final answer.")
MAX_RESULT_CHARS = 6000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments
    run: Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model sent arguments that are not JSON
    raw: str = ""


@dataclass
class Turn:
    calls: list[ToolCall]
    text: str  # any text the model wrote besides tool calls
    usage: Any
    model: str
    rejected: str = ""  # the provider refused this turn's output (e.g. a call to a tool that does not exist)


@dataclass
class Transcript:
    steps: list[dict[str, Any]] = field(default_factory=list)  # {tool, arguments, result_chars, error}

    def add(self, call: ToolCall, result: str, error: str | None = None) -> None:
        self.steps.append({"tool": call.name, "arguments": call.arguments, "result_chars": len(result),
                           "error": error})


def submit_tool(output_model: type[BaseModel]) -> dict[str, Any]:
    return {"name": SUBMIT, "description": "Give the final answer. Call this exactly once, when you are done.",
            "parameters": output_model.model_json_schema()}


def specs(tools: list[Any], output_model: type[BaseModel]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools] + \
        [submit_tool(output_model)]


async def run_tool(tools: dict[str, Any], call: ToolCall,
                   max_chars: int = MAX_RESULT_CHARS) -> tuple[str, str | None]:
    """The tool's result as text for the model, and an error message when it failed."""
    if call.arguments is None:
        return f"error: arguments are not valid JSON: {call.raw[:200]}", "bad arguments"
    tool = tools.get(call.name)
    if tool is None:
        return f"error: no tool named {call.name}; tools: {', '.join(tools)} or {SUBMIT}", "unknown tool"
    try:
        result = await tool.run(call.arguments)
    except Exception as e:  # noqa: BLE001 - the model sees the error and may try again
        return f"error: {type(e).__name__}: {str(e)[:300]}", type(e).__name__
    try:
        privacy.check(result)
    except privacy.PrivacyViolation as e:
        return f"withheld: the result looked like row-level data ({e})", "privacy"
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + " …(truncated)"
    return text, None


def tools_key(tools: list[Any]) -> str:
    return ",".join(sorted(t.name for t in tools))
