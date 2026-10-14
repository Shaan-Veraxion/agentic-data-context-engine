"""Project the fact log into Graphiti (graphiti-core on Neo4j 5.26).

Mapping:
- Entity          -> Graphiti EntityNode (label = entity type), uuid derived from the entity key
- Fact            -> Graphiti EntityEdge (RELATES_TO) from subject to object (or to itself for attribute facts),
                     name = fact kind, fact = readable text, valid_at = valid time, created_at = record time;
                     trust, status, confidence, agent and the JSON value are edge properties
- Evidence        -> Graphiti EpisodicNode (one per evidence reference) with MENTIONS edges to the entities
- Supersession    -> the old edge gets expired_at (transaction time) and status 'superseded'; nothing is deleted

Writes are deterministic: nodes and edges are saved directly. Graphiti's LLM-based extraction and deduplication
(add_episode / add_triplet) are not used for structured facts; they are reserved for documents in later stages.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")  # never send telemetry from ADCE installs

from adce_core.facts import Entity, Fact, FactStatus  # noqa: E402
from graphiti_core.driver.neo4j_driver import Neo4jDriver  # noqa: E402
from graphiti_core.edges import EntityEdge, EpisodicEdge  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode  # noqa: E402
from graphiti_core.search.search_filters import SearchFilters  # noqa: E402
from graphiti_core.search.search_utils import edge_fulltext_search  # noqa: E402

NS = uuid.UUID("6f1c3e2a-8d1b-4b7e-9a51-adce00000001")


def node_uuid(key: str) -> str:
    return str(uuid.uuid5(NS, f"entity:{key}"))


def edge_uuid(fact_id: str) -> str:
    return str(uuid.uuid5(NS, f"fact:{fact_id}"))


def episode_uuid(kind: str, ref: str) -> str:
    return str(uuid.uuid5(NS, f"evidence:{kind}:{ref}"))


def search_text(text: str) -> str:
    """Fact text plus a punctuation-split copy, so keys like sales.erp.orders_v2 match a search for orders_v2."""
    split = re.sub(r"[^0-9A-Za-z]+", " ", text)
    return f"{text}\n{split}"


def _label(t: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t):
        return t
    s = re.sub(r"[^0-9A-Za-z]+", " ", t).title().replace(" ", "")
    return s if s and s[0].isalpha() else f"T{s}"


def _scalar_props(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Neo4j properties must be scalars or lists of scalars; nested values are stored as JSON text."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = re.sub(r"\W", "_", f"{prefix}{k}")
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[key] = v
        else:
            out[key] = json.dumps(v, default=str, sort_keys=True)
    return out


class GraphitiProjector:
    def __init__(self, uri: str, user: str, password: str, embedder: EmbedderClient, group_id: str = "adce") -> None:
        self.driver = Neo4jDriver(uri=uri, user=user, password=password)
        self.embedder = embedder
        self.group_id = group_id

    async def init(self) -> None:
        # Neo4jDriver schedules index creation itself when built inside a running loop; wait for it then.
        task = getattr(self.driver, "_init_task", None)
        if task is not None:
            await task
        else:
            await self.driver.build_indices_and_constraints()

    async def close(self) -> None:
        await self.driver.close()

    # ------------------------------------------------------------------ apply events
    async def apply(self, topic: str, payload: dict[str, Any]) -> None:
        if topic == "entity.registered":
            await self._save_entity(Entity(**payload["entity"]))
        elif topic.startswith("fact."):
            await self._save_fact(Fact(**payload["fact"]))

    async def _save_entity(self, e: Entity) -> EntityNode:
        node = EntityNode(uuid=node_uuid(e.key), name=e.name, group_id=self.group_id, labels=[_label(e.type)],
                          summary=f"{e.type} {e.key}",
                          attributes={"key": e.key, "entity_type": e.type, **_scalar_props(e.attributes, "attr_")})
        await node.generate_name_embedding(self.embedder)
        await node.save(self.driver)
        return node

    async def _ensure_node(self, key: str) -> str:
        uid = node_uuid(key)
        records, _, _ = await self.driver.execute_query("MATCH (n:Entity {uuid: $uuid}) RETURN n.uuid AS uuid",
                                                         uuid=uid, routing_="r")
        if not records:
            await self._save_entity(Entity(key=key, type=key.split(":", 1)[0] or "Entity", name=key))
        return uid

    async def _save_fact(self, f: Fact) -> None:
        src = await self._ensure_node(f.subject)
        dst = await self._ensure_node(f.object) if f.object else src
        fid = f.id
        episodes = []
        for ev in f.evidence:
            ep = EpisodicNode(uuid=episode_uuid(ev.kind, ev.ref), name=ev.ref[:200], group_id=self.group_id,
                              source=EpisodeType.json, source_description=ev.kind,
                              content=json.dumps({"ref": ev.ref, **ev.detail}, default=str),
                              valid_at=f.recorded_at, entity_edges=[edge_uuid(fid)])
            await ep.save(self.driver)
            for target in {src, dst}:
                await EpisodicEdge(uuid=str(uuid.uuid5(NS, f"mentions:{ep.uuid}:{target}")), group_id=self.group_id,
                                   source_node_uuid=ep.uuid, target_node_uuid=target,
                                   created_at=f.recorded_at).save(self.driver)
            episodes.append(ep.uuid)
        edge = EntityEdge(
            uuid=edge_uuid(fid), group_id=self.group_id, source_node_uuid=src, target_node_uuid=dst,
            created_at=f.recorded_at, name=f.kind, fact=search_text(f.text), episodes=episodes,
            valid_at=f.valid_from or f.recorded_at,
            attributes={"fact_id": fid, "trust": f.trust.value, "status": f.status.value,
                        "confidence": f.confidence, "agent": f.agent, "agent_version": f.agent_version,
                        "value_json": json.dumps(f.value, default=str, sort_keys=True),
                        "supersedes": f.supersedes})
        await edge.generate_embedding(self.embedder)
        await edge.save(self.driver)
        if f.supersedes:
            await self.driver.execute_query(
                "MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() SET e.expired_at = $now, e.status = $status",
                uuid=edge_uuid(f.supersedes), now=f.recorded_at or datetime.now(UTC),
                status=FactStatus.SUPERSEDED.value)

    # ------------------------------------------------------------------ reads
    async def search_fact_ids(self, text: str, limit: int = 10, include_expired: bool = False) -> list[str]:
        query = re.sub(r"[^0-9A-Za-z]+", " ", text).strip()  # same split as search_text()
        if not query:
            return []
        edges = await edge_fulltext_search(self.driver, query, SearchFilters(), [self.group_id], limit * 3)
        out: list[str] = []
        for e in edges:
            if e.expired_at is not None and not include_expired:
                continue
            fid = e.attributes.get("fact_id") if e.attributes else None
            if fid and fid not in out:
                out.append(fid)
        return out[:limit]

    async def neighbourhood(self, key: str, limit: int = 50) -> list[dict[str, Any]]:
        """Current edges touching an entity, for context packs."""
        records, _, _ = await self.driver.execute_query(
            "MATCH (n:Entity {uuid: $uuid})-[e:RELATES_TO]-(m:Entity) WHERE e.expired_at IS NULL "
            "RETURN e.fact_id AS fact_id, e.name AS kind, m.key AS other, e.trust AS trust LIMIT $limit",
            uuid=node_uuid(key), limit=limit, routing_="r")
        return [dict(r) for r in records]

    async def wipe_group(self) -> None:
        """Remove this group's projection (used before a rebuild from the fact log)."""
        await self.driver.execute_query("MATCH (n {group_id: $g}) DETACH DELETE n", g=self.group_id)
