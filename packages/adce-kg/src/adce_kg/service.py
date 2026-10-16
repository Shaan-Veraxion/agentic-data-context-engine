"""KnowledgeService: the KGPort adapter behind kg-mcp.

Record: Postgres fact log (append-only, with outbox). Knowledge service: Graphiti on Neo4j, fed from the outbox.
Swapping the knowledge engine (for example to the evolved Brahmastra engine, ADR-12) means another KGPort
implementation; agents and kg-mcp do not change.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, LiteralString, cast

from adce_core.facts import Entity, Fact, FactStatus
from adce_core.ports import PolicyError, check_policy, decision_fact, invalidation_fact
from adce_core.settings import Settings

from .embedders import make_embedder
from .fact_log import PostgresFactLog
from .graphiti_projection import GraphitiProjector

WRITE_CYPHER = re.compile(r"(?i)\b(create|merge|delete|detach|set|remove|drop|foreach)\b|load\s+csv|"
                          r"call\s+(apoc\.(create|merge|refactor|periodic)|db\.(create|drop))")


class KnowledgeService:
    def __init__(self, log: PostgresFactLog, graph: GraphitiProjector | None, *, project_inline: bool = True) -> None:
        self.log = log
        self.graph = graph
        self.project_inline = project_inline
        self._lock = asyncio.Lock()

    @classmethod
    async def from_settings(cls, s: Settings, *, dsn: str | None = None, group_id: str | None = None,
                            with_graph: bool = True) -> KnowledgeService:
        if not s.tenant:  # isolation fails closed: no tenant binding, no knowledge service
            raise ValueError("ADCE_TENANT is not set: the knowledge service does not start without a tenant")
        log = await asyncio.to_thread(PostgresFactLog, dsn or s.pg_dsn, s.tenant)
        graph = None
        if with_graph:
            graph = GraphitiProjector(s.neo4j_uri, s.neo4j_user, s.neo4j_password, make_embedder(s),
                                      group_id or s.tenant)
            await graph.init()
        return cls(log, graph)

    async def close(self) -> None:
        await asyncio.to_thread(self.log.close)
        if self.graph:
            await self.graph.close()

    # ------------------------------------------------------------------ KGPort
    async def register_entity(self, entity: Entity) -> str:
        if await asyncio.to_thread(self.log.upsert_entity, entity):
            await self._after_write()
        return entity.key

    async def entity(self, key: str) -> Entity | None:
        return await asyncio.to_thread(self.log.entity, key)

    async def propose_fact(self, fact: Fact) -> str:
        """The write path: policy -> validator (conflicts with confirmed facts are flagged, never overwritten) ->
        trust gate (impact, review route on the event) -> commit + outbox in one transaction."""
        from adce_core.queries import conflict_fact, conflict_with, gate, impact_of

        check_policy(fact)
        if fact.tenant == "default" and self.log.tenant != "default":
            fact = fact.model_copy(update={"tenant": self.log.tenant})
        about = await self.facts(subject=fact.subject)
        fact = fact.model_copy(update={"impact": impact_of(fact, about)})
        wrote = await asyncio.to_thread(self.log.append, fact, "fact.proposed",
                                        {"review": gate(fact, fact.impact or 0)})
        if wrote:
            old = conflict_with(fact, about)
            if old is not None:
                await asyncio.to_thread(self.log.append, conflict_fact(fact, old), "conflict.flagged")
            await self._after_write()
        return fact.id

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str:
        old = await asyncio.to_thread(self.log.get, fact_id)
        if old is None:
            raise PolicyError(f"unknown fact {fact_id}")
        new = decision_fact(old, status, reviewer, correction)
        topic = "fact.confirmed" if status == FactStatus.CONFIRMED else f"fact.{status.value}"
        if await asyncio.to_thread(self.log.append, new, topic):
            await self._after_write()
        return new.id

    async def invalidate(self, fact_id: str, reason: str, by: str) -> str:
        old = await asyncio.to_thread(self.log.get, fact_id)
        if old is None:
            raise PolicyError(f"unknown fact {fact_id}")
        new = invalidation_fact(old, reason, by)
        if await asyncio.to_thread(self.log.append, new, "fact.superseded"):
            await self._after_write()
        return new.id

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]:
        return await asyncio.to_thread(self.log.query, kind=kind, subject=subject, current=current)

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]:
        if self.graph is None:
            return []
        ids = await self.graph.search_fact_ids(text, limit)
        return await asyncio.to_thread(self.log.get_many, ids)

    async def context(self, key: str, limit: int = 50, token_budget: int | None = None) -> dict[str, Any]:
        """A small context pack: the entity, its current facts, and its graph neighbourhood. With token_budget:
        trust-ranked and trimmed to the budget (adce_core.queries.context_pack)."""
        if token_budget is not None:
            from adce_core.queries import context_pack

            return await context_pack(self, key, token_budget)
        ent = await self.entity(key)
        own = await self.facts(subject=key)
        neigh = await self.graph.neighbourhood(key, limit) if self.graph else []
        return {"entity": ent.model_dump() if ent else None, "facts": [f.model_dump(mode="json") for f in own],
                "neighbourhood": neigh}

    async def readonly_cypher(self, query: str, params: dict[str, Any], limit: int = 200) -> list[dict[str, Any]]:
        """A read-only Cypher query on this tenant's projection: write clauses refused, $group_id bound to the
        tenant (the query must use it), read routing, at most `limit` rows."""
        if self.graph is None:
            raise PolicyError("no graph projection")
        if WRITE_CYPHER.search(query):
            raise PolicyError("only read queries: CREATE, MERGE, DELETE, SET, REMOVE, DROP, LOAD CSV, FOREACH and "
                              "procedure calls that write are refused")
        if "$group_id" not in query:
            raise PolicyError("the query must filter on $group_id (the tenant)")
        checked = cast(LiteralString, query)  # checked above: read clauses only, tenant-filtered
        recs, _, _ = await self.graph.driver.execute_query(checked, params={**params, "group_id": self.graph.group_id},
                                                           routing_="r")
        return [r.data() for r in recs[:limit]]

    async def events(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """The outbox after `after` (ADR-6): what the orchestrator's event consumer reads."""
        return await asyncio.to_thread(self.log.events_after, after, limit)

    # ------------------------------------------------------------------ projection
    async def _after_write(self) -> None:
        if self.project_inline and self.graph is not None:
            await self.project_pending()

    async def project_pending(self) -> int:
        """Apply pending outbox events to the graph, in order. Safe to call repeatedly and concurrently."""
        if self.graph is None:
            return 0
        async with self._lock:
            done = 0
            while True:
                rows = await asyncio.to_thread(self.log.pending)
                if not rows:
                    return done
                for r in rows:
                    await self.graph.apply(r["topic"], r["payload"])
                    await asyncio.to_thread(self.log.mark_projected, [r["seq"]])
                    done += 1

    async def rebuild_projection(self) -> int:
        """Drop and rebuild the graph from the fact log: the projection is derived, never the source of truth."""
        if self.graph is None:
            return 0
        await self.graph.wipe_group()
        events = await asyncio.to_thread(self.log.all_events)
        for r in events:
            await self.graph.apply(r["topic"], r["payload"])
        return len(events)
