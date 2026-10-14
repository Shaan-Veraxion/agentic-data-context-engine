"""The runtime contract every ADCE agent implements (ADR-4).

receive a job -> read context from the knowledge service -> do the work (code, AI, or both)
-> propose facts with evidence -> write artifacts -> return events and metrics for the trace.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .artifacts import ArtifactStore
from .facts import Entity, Fact
from .ports import DataPort, KGPort


class AgentKind(StrEnum):
    DETERMINISTIC = "D"
    HYBRID = "H"
    LLM = "L"


class AgentSpec(BaseModel):
    number: int | None  # position in the ADCE agent catalog (None for the orchestrator)
    name: str
    version: str
    kind: AgentKind
    triggers: list[str]  # events that start the agent
    emits: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)  # fact kinds it may propose
    routes: list[str] = Field(default_factory=list)  # model-gateway routes it may use


@dataclass
class AgentContext:
    kg: KGPort
    artifacts: ArtifactStore
    run_id: str
    app_id: str
    gateway: Any | None = None  # adce_gateway.ModelGateway (kept untyped to avoid a hard dependency)
    data: DataPort | None = None


@dataclass
class AgentResult:
    events: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)


class PolicyViolation(PermissionError):
    pass


class BaseAgent(ABC):
    spec: AgentSpec

    async def __call__(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        t0 = time.perf_counter()
        result = await self.run(job, ctx)
        result.metrics.setdefault("agent", f"{self.spec.name}@{self.spec.version}")
        result.metrics["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    @abstractmethod
    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult: ...

    async def propose(self, ctx: AgentContext, fact: Fact, result: AgentResult) -> str:
        if fact.kind not in self.spec.writes:
            raise PolicyViolation(f"{self.spec.name} may not write fact kind {fact.kind!r}")
        fact = fact.model_copy(update={"agent": self.spec.name, "agent_version": self.spec.version})
        fid = await ctx.kg.propose_fact(fact)
        result.facts.append(fid)
        return fid

    async def register(self, ctx: AgentContext, entity: Entity) -> str:
        return await ctx.kg.register_entity(entity)
