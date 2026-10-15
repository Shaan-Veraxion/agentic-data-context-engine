"""kg-mcp: the knowledge service's tools, served over MCP (FastMCP 3.x, streamable HTTP).

The only way agents (and, later, external AI clients) read or write knowledge. Writes pass the policy in
KGPort (evidence required, agents never assign 'confirmed'). Decisions are not exposed here: they come from
hitl-service. Agent identity will come from the caller's Keycloak service-account token (P3); until then the
caller names itself and the name is recorded on every fact.

Run:  uv run python -m kg_mcp.server  (listens on ADCE_KG_MCP_PORT, default 8701, path /mcp)
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from adce_core import feedback
from adce_core.facts import Entity, Evidence, Fact, Trust
from adce_core.ports import KGPort, PolicyError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

INSTRUCTIONS = (
    "ADCE knowledge service. Register entities, propose facts with evidence, and read facts and context packs. "
    "Facts are append-only; trust is documented, validated or inferred (only people confirm)."
)


class EvidenceIn(BaseModel):
    kind: str = Field(description="script_span | reload_log | qvd_header | query_result | export | upload")
    ref: str = Field(description="file:lines, log reference, query id, ...")
    detail: dict[str, Any] = Field(default_factory=dict)


class FactIn(BaseModel):
    kind: str
    subject: str
    object: str | None = None
    value: dict[str, Any]
    trust: Trust
    confidence: float = 1.0
    evidence: list[EvidenceIn]
    agent: str
    agent_version: str = "0.1.0"


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z_#$][A-Za-z0-9_#$.]*|\d+", text or "")}


def _fact_out(f: Fact) -> dict[str, Any]:
    return {"id": f.id, **f.model_dump(mode="json")}


def create_server(kg_factory: Callable[[], Awaitable[KGPort]]) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        kg = await kg_factory()
        try:
            yield {"kg": kg}
        finally:
            close = getattr(kg, "close", None)
            if close is not None:
                await close()

    mcp: FastMCP = FastMCP("kg-mcp", instructions=INSTRUCTIONS, lifespan=lifespan)

    def kg_of(ctx: Context) -> KGPort:
        return ctx.lifespan_context["kg"]

    @mcp.tool
    async def register_entity(key: str, type: str, name: str, ctx: Context,
                              attributes: dict[str, Any] | None = None) -> str:
        """Register an entity (idempotent). Keys are stable, e.g. app:<id>/table:<name>."""
        return await kg_of(ctx).register_entity(Entity(key=key, type=type, name=name, attributes=attributes or {}))

    @mcp.tool
    async def propose_fact(fact: FactIn, ctx: Context) -> dict[str, Any]:
        """Propose a fact with evidence. Returns its stable id. Re-proposing the same fact is a no-op."""
        try:
            fid = await kg_of(ctx).propose_fact(Fact(**fact.model_dump(exclude={"evidence"}),
                                                     evidence=[Evidence(**e.model_dump()) for e in fact.evidence]))
        except PolicyError as e:
            raise ToolError(f"policy: {e}") from e
        return {"fact_id": fid}

    @mcp.tool
    async def propose_facts(facts: list[FactIn], ctx: Context) -> dict[str, Any]:
        """Propose several facts at once (each passes the same policy). Returns their ids in order."""
        ids = []
        for fact in facts:
            try:
                ids.append(await kg_of(ctx).propose_fact(Fact(**fact.model_dump(exclude={"evidence"}),
                                                              evidence=[Evidence(**e.model_dump())
                                                                        for e in fact.evidence])))
            except PolicyError as e:
                raise ToolError(f"policy: {e} (fact {len(ids)} of the batch)") from e
        return {"fact_ids": ids}

    @mcp.tool
    async def register_entities(entities: list[dict[str, Any]], ctx: Context) -> list[str]:
        """Register several entities at once (idempotent)."""
        return [await kg_of(ctx).register_entity(Entity(**e)) for e in entities]

    @mcp.tool
    async def get_entity(key: str, ctx: Context) -> dict[str, Any] | None:
        """One entity by key."""
        e = await kg_of(ctx).entity(key)
        return e.model_dump() if e else None

    @mcp.tool
    async def get_facts(ctx: Context, kind: str | None = None, subject: str | None = None,
                        include_history: bool = False) -> list[dict[str, Any]]:
        """Current facts (or the full history) filtered by kind and/or subject."""
        return [_fact_out(f) for f in await kg_of(ctx).facts(kind=kind, subject=subject,
                                                               current=not include_history)]

    @mcp.tool
    async def search_context(text: str, ctx: Context, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search over current facts."""
        return [_fact_out(f) for f in await kg_of(ctx).search(text, limit=limit)]

    @mcp.tool
    async def find_similar_facts(kind: str, text: str, ctx: Context, limit: int = 5,
                                 field: str | None = None) -> list[dict[str, Any]]:
        """Current facts of one kind ranked by word overlap with `text` (in `value[field]`, or the whole value),
        e.g. kind=translation_memory, field=statement: earlier accepted translations of similar statements."""
        want = _words(text)
        scored = []
        for f in await kg_of(ctx).facts(kind=kind):
            have = _words(str(f.value.get(field)) if field else json.dumps(f.value, default=str))
            if want and have:
                score = len(want & have) / len(want | have)
                if score > 0:
                    scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        return [{"similarity": round(sc, 3), **_fact_out(f)} for sc, f in scored[:limit]]

    @mcp.tool
    async def get_context_pack(key: str, ctx: Context, limit: int = 50) -> dict[str, Any]:
        """The entity, its current facts and its graph neighbourhood, for an agent's prompt or reasoning."""
        kg = kg_of(ctx)
        builder = getattr(kg, "context", None)
        if builder is not None:
            return await builder(key, limit)
        ent = await kg.entity(key)
        return {"entity": ent.model_dump() if ent else None,
                "facts": [_fact_out(f) for f in await kg.facts(subject=key)], "neighbourhood": []}

    @mcp.tool
    async def get_feedback_history(ctx: Context, text: str | None = None, kinds: list[str] | None = None,
                                   app_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """What reviewers decided before: each (proposal, decision, edit, reason), most similar to `text` first.
        kinds: report_sql (SQL approved/rejected), translation_override (SQL a reviewer wrote for a statement),
        bi_gap_accepted, variable_value, resolves_to, bi_issue_acknowledged. app_id limits to one application."""
        return await feedback.feedback_history(kg_of(ctx), kinds=kinds, app_id=app_id, text=text, limit=limit)

    @mcp.tool
    async def subscribe_events(ctx: Context, after: int = 0, limit: int = 500,
                               topics: list[str] | None = None) -> dict[str, Any]:
        """Knowledge events after position `after` (fact.proposed, fact.confirmed, fact.rejected,
        entity.registered), in order, for the orchestrator. Poll with the returned `last`."""
        log = getattr(kg_of(ctx), "events", None)
        rows = await log(after, limit) if log is not None else []
        last = rows[-1]["seq"] if rows else after
        return {"events": [r for r in rows if not topics or r["topic"] in topics], "last": last}

    return mcp


async def _knowledge_service() -> KGPort:
    from adce_core.settings import get_settings
    from adce_kg import KnowledgeService

    return await KnowledgeService.from_settings(get_settings())


def main() -> None:
    port = int(os.environ.get("ADCE_KG_MCP_PORT", "8701"))
    create_server(_knowledge_service).run(transport="http", host="0.0.0.0", port=port, path="/mcp")


if __name__ == "__main__":
    main()
