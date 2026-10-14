"""KnowledgeService: the KGPort adapter behind kg-mcp.

Record: Postgres fact log (append-only, with outbox). Knowledge service: Graphiti on Neo4j, fed from the outbox.
Swapping the knowledge engine (for example to the evolved Brahmastra engine, ADR-12) means another KGPort
implementation; agents and kg-mcp do not change.
"""
from __future__ import annotations

import asyncio
from typing import Any

from adce_core.facts import Entity, Fact, FactStatus
from adce_core.ports import PolicyError, check_policy, decision_fact
from adce_core.settings import Settings

from .embedders import make_embedder
from .fact_log import PostgresFactLog
from .graphiti_projection import GraphitiProjector


class KnowledgeService:
    def __init__(self, log: PostgresFactLog, graph: GraphitiProjector | None, *, project_inline: bool = True) -> None:
        self.log = log
        self.graph = graph
        self.project_inline = project_inline
        self._lock = asyncio.Lock()

    @classmethod
    async def from_settings(cls, s: Settings, *, dsn: str | None = None, group_id: str | None = None,
                            with_graph: bool = True) -> KnowledgeService:
        log = await asyncio.to_thread(PostgresFactLog, dsn or s.pg_dsn)
        graph = None
        if with_graph:
            graph = GraphitiProjector(s.neo4j_uri, s.neo4j_user, s.neo4j_password, make_embedder(s),
                                      group_id or s.kg_group)
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
        check_policy(fact)
        if await asyncio.to_thread(self.log.append, fact, "fact.proposed"):
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

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]:
        return await asyncio.to_thread(self.log.query, kind=kind, subject=subject, current=current)

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]:
        if self.graph is None:
            return []
        ids = await self.graph.search_fact_ids(text, limit)
        return await asyncio.to_thread(self.log.get_many, ids)

    async def context(self, key: str, limit: int = 50) -> dict[str, Any]:
        """A small context pack: the entity, its current facts, and its graph neighbourhood."""
        ent = await self.entity(key)
        own = await self.facts(subject=key)
        neigh = await self.graph.neighbourhood(key, limit) if self.graph else []
        return {"entity": ent.model_dump() if ent else None, "facts": [f.model_dump(mode="json") for f in own],
                "neighbourhood": neigh}

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
