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
from datetime import datetime
from typing import Any

from adce_core import feedback, queries
from adce_core.facts import Entity, Evidence, Fact, FactStatus, Trust
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
    scope: str | None = None
    run_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


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
    async def update_fact(fact_id: str, reason: str, agent: str, ctx: Context) -> dict[str, Any]:
        """Stop believing a fact: an invalidation row is appended (nothing is deleted); current reads hide both.
        `agent` names who invalidates it and `reason` why."""
        try:
            return {"fact_id": await kg_of(ctx).invalidate(fact_id, reason, agent)}
        except (PolicyError, KeyError) as e:
            raise ToolError(f"policy: {e}") from e

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
    async def get_context_pack(key: str, ctx: Context, limit: int = 50, token_budget: int | None = None
                               ) -> dict[str, Any]:
        """What to know about an entity before working on it. With token_budget: its facts and those of everything
        under it, highest trust first and newest first, trimmed to the budget (deterministic). Without: the entity,
        its current facts and its graph neighbourhood (up to `limit` edges)."""
        kg = kg_of(ctx)
        if token_budget is not None:
            return await queries.context_pack(kg, key, token_budget)
        builder = getattr(kg, "context", None)
        if builder is not None:
            return await builder(key, limit)
        ent = await kg.entity(key)
        return {"entity": ent.model_dump() if ent else None,
                "facts": [_fact_out(f) for f in await kg.facts(subject=key)], "neighbourhood": []}

    @mcp.tool
    async def get_neighbors(key: str, ctx: Context) -> list[dict[str, Any]]:
        """Current facts about an entity or pointing at it (its neighbourhood in the knowledge graph)."""
        return [_fact_out(f) for f in await queries.neighbors(kg_of(ctx), key)]

    @mcp.tool
    async def get_source_context(key: str, ctx: Context) -> dict[str, Any]:
        """An entity (an application, a source) with its facts and those of everything under it (key/...)."""
        return await queries.source_context(kg_of(ctx), key)

    @mcp.tool
    async def get_lineage(key: str, ctx: Context, direction: str = "upstream", depth: int = 5) -> list[dict[str, Any]]:
        """Lineage of a table (app:<id>/table:<name>): upstream = what it is derived from (sources, QVDs, tables),
        downstream = what is derived from it, up to `depth` steps."""
        return await queries.lineage(kg_of(ctx), key, direction, depth)

    @mcp.tool
    async def find_join_paths(a: str, b: str, ctx: Context, max_hops: int = 4) -> list[list[dict[str, Any]]]:
        """Shortest association paths between two tables (Qlik associations on same-named fields), each hop with
        its keys and trust; trusted links first."""
        return await queries.join_paths(kg_of(ctx), a, b, max_hops)

    @mcp.tool
    async def add_context(subject: str, text: str, agent: str, ctx: Context, source: str = "agent") -> dict[str, Any]:
        """Attach a piece of documented context (a definition, a note from a document) to an entity."""
        return await propose_fact(FactIn(kind="context", subject=subject, value={"text": text, "source": source},
                                         trust=Trust.DOCUMENTED, agent=agent,
                                         evidence=[EvidenceIn(kind="doc_span", ref=source)]), ctx)

    @mcp.tool
    async def link_entities(subject: str, object: str, relation: str, agent: str, ctx: Context,
                            evidence_ref: str = "") -> dict[str, Any]:
        """Link two entities (e.g. a Qlik source to a Dremio dataset candidate) with a named relation."""
        return await propose_fact(FactIn(kind="entity_link", subject=subject, object=object,
                                         value={"relation": relation}, trust=Trust.INFERRED, agent=agent,
                                         evidence=[EvidenceIn(kind="link",
                                                              ref=evidence_ref or f"{subject}->{object}")]),
                                  ctx)

    @mcp.tool
    async def flag_conflict(fact_a: str, fact_b: str, reason: str, agent: str, ctx: Context,
                            subject: str = "") -> dict[str, Any]:
        """Record that two facts disagree (never overwrite either): a conflict fact a reviewer resolves."""
        return await propose_fact(FactIn(kind="conflict", subject=subject or fact_a,
                                         value={"facts": [fact_a, fact_b], "reason": reason}, trust=Trust.INFERRED,
                                         agent=agent, evidence=[EvidenceIn(kind="fact", ref=fact_a),
                                                                EvidenceIn(kind="fact", ref=fact_b)]), ctx)

    @mcp.tool
    async def record_feedback(fact_id: str, decision: str, reviewer: str, caller: str, ctx: Context,
                              correction: dict[str, Any] | None = None) -> dict[str, Any]:
        """A reviewer's decision on a fact (confirmed or rejected). Only the review service may call it (caller
        must be hitl-service); agents never confirm knowledge."""
        if caller != "hitl-service":
            raise ToolError("policy: only hitl-service records reviewer decisions")
        try:
            status = FactStatus(decision)
            return {"fact_id": await kg_of(ctx).decide(fact_id, status, reviewer, correction)}
        except (PolicyError, ValueError, KeyError) as e:
            raise ToolError(f"policy: {e}") from e

    @mcp.tool
    async def run_readonly_cypher(query: str, ctx: Context, params: dict[str, Any] | None = None,
                                  limit: int = 200) -> list[dict[str, Any]]:
        """A read-only Cypher query on the graph projection (for the join planner and questions). Write clauses are
        refused; the query must filter on $group_id (the tenant), which is bound by the server; at most `limit`
        rows."""
        run = getattr(kg_of(ctx), "readonly_cypher", None)
        if run is None:
            raise ToolError("this knowledge store has no graph projection")
        try:
            return await run(query, params or {}, limit)
        except PolicyError as e:
            raise ToolError(f"policy: {e}") from e

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
    from adce_core import telemetry

    telemetry.setup("kg-mcp")
    port = int(os.environ.get("ADCE_KG_MCP_PORT", "8701"))
    create_server(_knowledge_service).run(transport="http", host="0.0.0.0", port=port, path="/mcp")


if __name__ == "__main__":
    main()
