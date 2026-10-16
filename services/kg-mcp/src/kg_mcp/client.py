"""McpKG: the knowledge port over MCP. Agents read and write knowledge through kg-mcp (ADR-2), whether the server
runs as a service (streamable HTTP) or in the same process (FastMCP's in-memory transport, for tests and the CLI).

Two uses:
- as a KGPort (register_entity, propose_fact, facts, search, ...), so agents keep their code and gain the MCP
  boundary: the server's policy runs on every write;
- as model tools: `tools([...])` exposes chosen kg-mcp read tools to a model through the model gateway, so an
  LLM step can look up context itself (context packs, similar earlier answers) instead of only what its prompt holds.

    async with McpKG.serving(MemoryKG()) as kg: ...          # in-process server
    async with McpKG.connect("http://kg-mcp:8701/mcp") as kg: ...
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from adce_core.facts import Entity, Fact, FactStatus
from adce_core.ports import KGPort, PolicyError, check_policy
from fastmcp import Client
from fastmcp.exceptions import ToolError

READ_TOOLS = ("get_entity", "get_facts", "search_context", "get_context_pack", "find_similar_facts",
              "get_feedback_history")  # offered to models by default (recorded tool-use answers depend on this set)
ALL_READ_TOOLS = (*READ_TOOLS, "get_neighbors", "get_source_context", "get_lineage", "find_join_paths",
                  "run_readonly_cypher")  # any of these may be offered to a model on request


@dataclass
class McpTool:
    """A kg-mcp tool offered to a model (same shape as adce_gateway.tools.Tool)."""

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[Any]]


def _fact(d: dict[str, Any]) -> Fact:
    return Fact.model_validate({k: v for k, v in d.items() if k not in ("id", "similarity")})


class McpKG:
    """Writes are buffered and sent in batches (propose_facts / register_entities): a fact's id is a hash of its
    content, so it is known before the server answers, and the policy is checked here first so a bad fact fails
    at once, in the agent that proposed it. The server checks every fact again. Reads flush the buffer first, so
    an agent always reads what it wrote."""

    BATCH = 200

    def __init__(self, client: Client) -> None:  # type: ignore[type-arg]
        self.client = client
        self.calls = 0
        self._facts: list[dict[str, Any]] = []
        self._entities: list[dict[str, Any]] = []

    @classmethod
    @asynccontextmanager
    async def serving(cls, kg: KGPort) -> AsyncIterator[McpKG]:
        from .server import create_server

        async def factory() -> KGPort:
            return kg

        async with Client(create_server(factory)) as c:
            kg = cls(c)
            yield kg
            await kg.flush()

    @classmethod
    @asynccontextmanager
    async def connect(cls, url: str) -> AsyncIterator[McpKG]:
        async with Client(url) as c:
            kg = cls(c)
            yield kg
            await kg.flush()

    async def flush(self) -> None:
        if self._entities:
            batch, self._entities = self._entities, []
            await self._send("register_entities", {"entities": batch})
        if self._facts:
            batch, self._facts = self._facts, []
            await self._send("propose_facts", {"facts": batch})

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        await self.flush()
        return await self._send(name, args)

    async def _send(self, name: str, args: dict[str, Any]) -> Any:
        self.calls += 1
        try:
            r = await self.client.call_tool(name, args)
        except ToolError as e:
            if str(e).startswith("policy:"):
                raise PolicyError(str(e).removeprefix("policy:").strip()) from e
            raise
        if r.structured_content is not None:
            sc = r.structured_content
            return sc["result"] if set(sc) == {"result"} else sc
        return r.data

    # ------------------------------------------------------------------ KGPort
    async def register_entity(self, entity: Entity) -> str:
        self._entities.append(entity.model_dump())
        if len(self._entities) >= self.BATCH:
            await self.flush()
        return entity.key

    async def entity(self, key: str) -> Entity | None:
        d = await self._call("get_entity", {"key": key})
        return Entity.model_validate(d) if d else None

    async def propose_fact(self, fact: Fact) -> str:
        check_policy(fact)
        self._facts.append(fact.model_dump(mode="json", include={
            "kind", "subject", "object", "value", "trust", "confidence", "evidence", "agent", "agent_version",
            "scope", "run_id", "model", "prompt_version", "valid_from", "valid_to"}, exclude_none=True))
        if len(self._facts) >= self.BATCH:
            await self.flush()
        return fact.id

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str:
        raise PolicyError("decisions are made in hitl-service, not through kg-mcp")

    async def invalidate(self, fact_id: str, reason: str, by: str) -> str:
        await self.flush()
        return (await self._call("update_fact", {"fact_id": fact_id, "reason": reason, "agent": by}))["fact_id"]

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]:
        rows = await self._call("get_facts", {"kind": kind, "subject": subject, "include_history": not current})
        return [_fact(d) for d in rows or []]

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]:
        return [_fact(d) for d in await self._call("search_context", {"text": text, "limit": limit}) or []]

    async def context(self, key: str, limit: int = 50, token_budget: int | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"key": key, "limit": limit}
        if token_budget is not None:
            args["token_budget"] = token_budget
        return await self._call("get_context_pack", args)

    async def neighbors(self, key: str) -> list[Fact]:
        return [_fact(d) for d in await self._call("get_neighbors", {"key": key}) or []]

    async def source_context(self, key: str) -> dict[str, Any]:
        return await self._call("get_source_context", {"key": key})

    async def lineage(self, key: str, direction: str = "upstream", depth: int = 5) -> list[dict[str, Any]]:
        return await self._call("get_lineage", {"key": key, "direction": direction, "depth": depth}) or []

    async def join_paths(self, a: str, b: str, max_hops: int = 4) -> list[list[dict[str, Any]]]:
        return await self._call("find_join_paths", {"a": a, "b": b, "max_hops": max_hops}) or []

    async def add_context(self, subject: str, text: str, agent: str, source: str = "agent") -> str:
        await self.flush()
        return (await self._call("add_context", {"subject": subject, "text": text, "agent": agent,
                                                 "source": source}))["fact_id"]

    async def link_entities(self, subject: str, obj: str, relation: str, agent: str) -> str:
        await self.flush()
        return (await self._call("link_entities", {"subject": subject, "object": obj, "relation": relation,
                                                   "agent": agent}))["fact_id"]

    async def flag_conflict(self, fact_a: str, fact_b: str, reason: str, agent: str) -> str:
        await self.flush()
        return (await self._call("flag_conflict", {"fact_a": fact_a, "fact_b": fact_b, "reason": reason,
                                                   "agent": agent}))["fact_id"]

    async def feedback_history(self, *, kinds: list[str] | None = None, app_id: str | None = None,
                               text: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        return await self._call("get_feedback_history", {"kinds": kinds, "app_id": app_id, "text": text,
                                                         "limit": limit}) or []

    async def events(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return (await self._call("subscribe_events", {"after": after, "limit": limit}))["events"]

    # ------------------------------------------------------------------ model tools
    async def tools(self, allow: tuple[str, ...] = READ_TOOLS, max_chars: int = 6000) -> list[McpTool]:
        """kg-mcp tools for a model; only read tools may be offered (models never write knowledge directly)."""
        bad = set(allow) - set(ALL_READ_TOOLS)
        if bad:
            raise PolicyError(f"only read tools may be given to a model: {sorted(bad)}")
        out = []
        for t in await self.client.list_tools():
            if t.name not in allow:
                continue
            params = dict(t.inputSchema or {"type": "object", "properties": {}})

            def make(name: str) -> Callable[[dict[str, Any]], Awaitable[Any]]:
                async def run(args: dict[str, Any]) -> Any:
                    res = await self._call(name, args)
                    text = json.dumps(res, default=str)
                    return res if len(text) <= max_chars else text[:max_chars] + " …(truncated)"
                return run

            out.append(McpTool(t.name, t.description or t.name, params, make(t.name)))
        return out
