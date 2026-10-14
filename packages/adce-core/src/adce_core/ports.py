"""Ports: the boundaries to the knowledge service and to data sources.

Agents depend only on these protocols.
- KGPort: implemented by adce_kg.KnowledgeService (Postgres fact log + Graphiti on Neo4j) and, for unit tests and
  offline runs, by MemoryKG below. Brahmastra (ADR-12) plugs in later as another implementation.
- DataPort: DuckDBSource is the local venue now; DremioSource arrives in phase PD.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .facts import AGENT_MAX_TRUST, Entity, Fact, FactStatus, Trust, Venue


class PolicyError(ValueError):
    pass


def check_policy(fact: Fact) -> None:
    """Write rules every KGPort implementation enforces (kg-mcp policy engine, first version)."""
    if fact.trust not in AGENT_MAX_TRUST:
        raise PolicyError(f"agents may not assign trust {fact.trust}")
    if not fact.evidence:
        raise PolicyError("a fact needs at least one piece of evidence")
    if not fact.agent:
        raise PolicyError("a fact must name the agent that proposed it")
    if not 0.0 <= fact.confidence <= 1.0:
        raise PolicyError("confidence must be between 0 and 1")
    if fact.status != FactStatus.PROPOSED:
        raise PolicyError("agents may only propose facts; decisions go through decide()")


def decision_fact(old: Fact, status: FactStatus, reviewer: str, correction: dict[str, Any] | None) -> Fact:
    """The fact appended when a person decides on a proposal. The old fact stays in the log, superseded."""
    if status == FactStatus.PROPOSED or status == FactStatus.SUPERSEDED:
        raise PolicyError(f"not a decision: {status}")
    value = {**old.value, **(correction or {}), "_decision": status.value, "_decided_by": reviewer}
    return old.model_copy(update={
        "status": status,
        "trust": Trust.CONFIRMED if status == FactStatus.CONFIRMED else old.trust,
        "supersedes": old.id,
        "agent": f"reviewer:{reviewer}",
        "value": value,
    }, deep=True)


class KGPort(Protocol):
    async def register_entity(self, entity: Entity) -> str: ...

    async def entity(self, key: str) -> Entity | None: ...

    async def propose_fact(self, fact: Fact) -> str: ...

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str: ...

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]: ...

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]: ...


class MemoryKG:
    """Append-only fact log in memory, with the same policy rules as the knowledge service."""

    def __init__(self) -> None:
        self._log: list[Fact] = []
        self._by_id: dict[str, Fact] = {}
        self._entities: dict[str, Entity] = {}

    async def register_entity(self, entity: Entity) -> str:
        self._entities.setdefault(entity.key, entity)
        return entity.key

    async def entity(self, key: str) -> Entity | None:
        return self._entities.get(key)

    async def propose_fact(self, fact: Fact) -> str:
        check_policy(fact)
        if fact.id in self._by_id:  # idempotent re-proposal
            return fact.id
        self._log.append(fact)
        self._by_id[fact.id] = fact
        return fact.id

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str:
        new = decision_fact(self._by_id[fact_id], status, reviewer, correction)
        self._log.append(new)
        self._by_id[new.id] = new
        return new.id

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]:
        superseded = {f.supersedes for f in self._log if f.supersedes}
        return [f for f in self._log
                if not (current and f.id in superseded)
                and (kind is None or f.kind == kind)
                and (subject is None or f.subject == subject)]

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]:
        t = text.lower()
        hits = [f for f in await self.facts() if t in f.text.lower()]
        return hits[:limit]

    @property
    def log(self) -> list[Fact]:
        return list(self._log)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    venue: Venue
    sql: str
    meta: dict[str, Any] = field(default_factory=dict)


class DataPort(Protocol):
    venue: Venue
    dialect: str  # sqlglot dialect name of the SQL this port executes

    def run_query(self, sql: str, *, label: str, max_rows: int = 10_000) -> QueryResult: ...

    def has_table(self, name: str) -> bool: ...
