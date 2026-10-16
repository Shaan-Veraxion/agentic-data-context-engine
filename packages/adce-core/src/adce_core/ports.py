"""Ports: the boundaries to the knowledge service and to data sources.

Agents depend only on these protocols.
- KGPort: implemented by adce_kg.KnowledgeService (Postgres fact log + Graphiti on Neo4j) and, for unit tests and
  offline runs, by MemoryKG below. Brahmastra (ADR-12) plugs in later as another implementation.
- DataPort: DuckDBSource is the local venue now; DremioSource arrives in phase PD.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .fact_schemas import problems
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
    bad = problems(fact.kind, fact.value)  # the fact kind's contract (fact_schemas)
    if bad:
        raise PolicyError("; ".join(bad))


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


def invalidation_fact(old: Fact, reason: str, by: str) -> Fact:
    """The row appended when a fact stops being believed: status superseded, invalidated_at now, pointing at it.
    Nothing is deleted; current reads hide both."""
    from datetime import UTC, datetime

    return old.model_copy(update={"status": FactStatus.SUPERSEDED, "supersedes": old.id, "agent": by,
                                  "invalidated_at": datetime.now(UTC),
                                  "value": {**old.value, "_invalidated": reason}}, deep=True)


class TenantMismatch(PolicyError):
    pass


class KGPort(Protocol):
    async def register_entity(self, entity: Entity) -> str: ...

    async def entity(self, key: str) -> Entity | None: ...

    async def propose_fact(self, fact: Fact) -> str: ...

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str: ...

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]: ...

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]: ...

    async def invalidate(self, fact_id: str, reason: str, by: str) -> str: ...


class EventLog(Protocol):
    """The outbox (ADR-6): every write's event, in order. Consumers keep their own position (seq)."""

    async def events(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]: ...


class MemoryKG:
    """Append-only fact log in memory, with the same policy rules as the knowledge service. Bound to one tenant:
    facts of another tenant are refused (fail closed)."""

    def __init__(self, tenant: str = "default") -> None:
        self.tenant = tenant
        self._log: list[Fact] = []
        self._by_id: dict[str, Fact] = {}
        self._entities: dict[str, Entity] = {}
        self._outbox: list[dict[str, Any]] = []  # same events as the Postgres outbox: {seq, topic, payload}

    def _emit(self, topic: str, payload: dict[str, Any]) -> None:
        from .events import outbox_problems

        bad = outbox_problems(topic, payload)
        if bad:
            raise PolicyError("; ".join(bad))
        self._outbox.append({"seq": len(self._outbox) + 1, "topic": topic, "payload": payload})

    async def register_entity(self, entity: Entity) -> str:
        if entity.key not in self._entities:
            self._entities[entity.key] = entity
            self._emit("entity.registered", {"entity": entity.model_dump(mode="json")})
        return entity.key

    async def entity(self, key: str) -> Entity | None:
        return self._entities.get(key)

    def _bind(self, fact: Fact) -> Fact:
        if fact.tenant == "default" and self.tenant != "default":
            fact = fact.model_copy(update={"tenant": self.tenant})
        if fact.tenant != self.tenant:
            raise TenantMismatch(f"a fact of tenant {fact.tenant!r} in the store of tenant {self.tenant!r}")
        return fact

    async def propose_fact(self, fact: Fact) -> str:
        """The write path: policy, then the validator (a contradiction of a confirmed fact is a conflict, kept
        and flagged), then the trust gate (impact and review route, on the event), then the append."""
        from .queries import conflict_fact, conflict_with, gate, impact_of

        check_policy(fact)
        fact = self._bind(fact)
        if fact.id in self._by_id:  # idempotent re-proposal
            return fact.id
        about = await self.facts(subject=fact.subject)
        fact = fact.model_copy(update={"impact": impact_of(fact, about)})
        self._log.append(fact)
        self._by_id[fact.id] = fact
        self._emit("fact.proposed", {"fact": fact.model_dump(mode="json"), "fact_id": fact.id,
                                     "review": gate(fact, fact.impact or 0)})
        old = conflict_with(fact, about)
        if old is not None:
            c = self._bind(conflict_fact(fact, old))
            if c.id not in self._by_id:
                self._log.append(c)
                self._by_id[c.id] = c
                self._emit("conflict.flagged", {"fact": c.model_dump(mode="json"), "fact_id": c.id})
        return fact.id

    async def decide(self, fact_id: str, status: FactStatus, reviewer: str,
                     correction: dict[str, Any] | None = None) -> str:
        new = decision_fact(self._by_id[fact_id], status, reviewer, correction)
        if new.id not in self._by_id:
            self._log.append(new)
            self._by_id[new.id] = new
            self._emit("fact.confirmed" if status == FactStatus.CONFIRMED else f"fact.{status.value}",
                       {"fact": new.model_dump(mode="json"), "fact_id": new.id})
        return new.id

    async def invalidate(self, fact_id: str, reason: str, by: str) -> str:
        new = invalidation_fact(self._by_id[fact_id], reason, by)
        if new.id not in self._by_id:
            self._log.append(new)
            self._by_id[new.id] = new
            self._emit("fact.superseded", {"fact": new.model_dump(mode="json"), "fact_id": new.id})
        return new.id

    async def facts(self, *, kind: str | None = None, subject: str | None = None,
                    current: bool = True) -> list[Fact]:
        superseded = {f.supersedes for f in self._log if f.supersedes}
        return [f for f in self._log
                if not (current and (f.id in superseded or f.status == FactStatus.SUPERSEDED))
                and (kind is None or f.kind == kind)
                and (subject is None or f.subject == subject)]

    async def search(self, text: str, *, limit: int = 10) -> list[Fact]:
        t = text.lower()
        hits = [f for f in await self.facts() if t in f.text.lower()]
        return hits[:limit]

    async def events(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return self._outbox[after:after + limit]

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
