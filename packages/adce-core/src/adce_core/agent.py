"""The runtime contract every ADCE agent implements (ADR-4).

receive a job -> read context from the knowledge service -> do the work (code, AI, or both)
-> propose facts with evidence -> write artifacts -> return events and metrics for the trace.

The spec (ADR-1 #1) says what an agent may do, and BaseAgent.bind() enforces it on every call:
- writes: the fact kinds it may propose (propose() refuses others);
- tools: the knowledge-service tools it may call (GuardedKG refuses others, e.g. record_feedback, which only the
  review service may call);
- routes and budget: the model routes it may use, and at most budget.max_calls calls / budget.max_usd per run
  (BudgetedGateway; past the budget a call raises BudgetExceeded, which agents treat like an unavailable provider);
- emits: the events it may emit (checked against the declared events, adce_core.events).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
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


# knowledge-service (kg-mcp) tools: every agent may read and propose; the rest are granted per agent
READ_TOOLS = ["get_facts", "get_entity", "search_context", "get_context_pack", "get_neighbors", "get_lineage",
              "get_source_context", "find_join_paths"]
WRITE_TOOLS = ["propose_fact", "register_entity", "add_context", "link_entities", "flag_conflict"]


class AgentBudget(BaseModel):
    max_calls: int | None = None  # model calls per run
    max_usd: float | None = None  # model spend per run


class AgentSpec(BaseModel):
    number: int | None  # position in the ADCE agent catalog (None for the orchestrator)
    name: str
    version: str
    kind: AgentKind
    triggers: list[str]  # events that start the agent
    emits: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)  # fact kinds it may propose
    routes: list[str] = Field(default_factory=list)  # model-gateway routes it may use
    group: str = "reversesql"  # agent group (ADR-11: the BI adapters are a sub-group of reversesql)
    subgroup: str | None = None  # e.g. bi_adapters
    tools: list[str] = Field(default_factory=lambda: [*READ_TOOLS, *WRITE_TOOLS])  # knowledge tools it may call
    hitl_modes: list[str] = Field(default_factory=list)  # seed | ask | gate | queue | correct
    budget: AgentBudget = Field(default_factory=AgentBudget)
    context_budget: int = 2000  # tokens of the application's context pack read before running (0: none)


@dataclass
class AgentContext:
    kg: KGPort
    artifacts: ArtifactStore
    run_id: str
    app_id: str
    gateway: Any | None = None  # adce_gateway.ModelGateway (kept untyped to avoid a hard dependency)
    data: DataPort | None = None
    context: dict[str, Any] | None = None  # the context pack the agent read first (ADR-4), set by __call__


@dataclass
class AgentResult:
    events: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)


class PolicyViolation(PermissionError):
    pass


class BudgetExceeded(RuntimeError):
    """An agent's budget for the run is used (same name as the gateway's: agents treat it the same way)."""


# KGPort method -> the kg-mcp tool it is (None: always allowed)
TOOL_OF = {"register_entity": "register_entity", "entity": "get_entity", "propose_fact": "propose_fact",
           "decide": "record_feedback", "facts": "get_facts", "search": "search_context",
           "context": "get_context_pack", "feedback_history": "get_feedback_history", "events": "subscribe_events",
           "tools": "model_tools", "neighbors": "get_neighbors", "lineage": "get_lineage",
           "source_context": "get_source_context", "join_paths": "find_join_paths", "add_context": "add_context",
           "link_entities": "link_entities", "flag_conflict": "flag_conflict", "invalidate": "update_fact",
           "readonly_cypher": "run_readonly_cypher"}


class GuardedKG:
    """The knowledge port as one agent may use it: calls to tools outside spec.tools are refused."""

    def __init__(self, inner: Any, spec: AgentSpec) -> None:
        self._inner, self._spec = inner, spec

    def __getattr__(self, name: str) -> Any:
        tool = TOOL_OF.get(name)
        if tool is not None and tool not in self._spec.tools:
            raise PolicyViolation(f"{self._spec.name} may not call the knowledge tool {tool!r}")
        return getattr(self._inner, name)


class BudgetedGateway:
    """The model gateway as one agent may use it in a run: its routes, within its budget."""

    def __init__(self, inner: Any, spec: AgentSpec) -> None:
        self._inner, self._spec = inner, spec
        self.calls, self.spent_usd = 0, 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def call(self, route: str, inputs: dict[str, Any], output_model: Any, **kw: Any) -> Any:
        b = self._spec.budget
        if route not in self._spec.routes:
            raise PolicyViolation(f"{self._spec.name} may not use the model route {route!r}")
        if b.max_calls is not None and self.calls >= b.max_calls:
            raise BudgetExceeded(f"{self._spec.name}: {b.max_calls} model calls per run used")
        if b.max_usd is not None and self.spent_usd >= b.max_usd:
            raise BudgetExceeded(f"{self._spec.name}: {b.max_usd} USD per run used")
        r = await self._inner.call(route, inputs, output_model, **kw)
        self.calls += 1
        self.spent_usd += float(getattr(r, "cost_usd", 0.0) or 0.0)
        return r


async def read_context(kg: Any, key: str, token_budget: int) -> dict[str, Any]:
    """A token-budgeted context pack: from the knowledge service when it builds one (kg-mcp: one call), else built
    here from its facts (adce_core.queries.context_pack)."""
    from .queries import context_pack

    build = getattr(kg, "context", None)
    if build is not None:
        try:
            return await build(key, token_budget=token_budget)
        except TypeError:  # a store whose context() takes no budget
            pass
    return await context_pack(kg, key, token_budget)


class BaseAgent(ABC):
    spec: AgentSpec

    async def __call__(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        t0 = time.perf_counter()
        ctx = self.bind(ctx)
        if self.spec.context_budget and "get_context_pack" in self.spec.tools and ctx.context is None:
            # ADR-4: read the context pack first
            ctx = replace(ctx, context=await read_context(ctx.kg, f"app:{ctx.app_id}", self.spec.context_budget))
        result = await self.run(job, ctx)
        if ctx.context is not None:
            result.metrics.setdefault("context_facts", len(ctx.context.get("facts", [])))
        self.check_events(result.events)
        result.metrics.setdefault("agent", f"{self.spec.name}@{self.spec.version}")
        result.metrics["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    def bind(self, ctx: AgentContext) -> AgentContext:
        """The context this agent runs with: the knowledge port limited to its tools, the gateway to its routes and
        budget. Graph nodes that call an agent's steps directly bind the context the same way."""
        if isinstance(ctx.kg, GuardedKG) and getattr(ctx.kg, "_spec", None) is self.spec:
            return ctx
        kg = ctx.kg._inner if isinstance(ctx.kg, GuardedKG) else ctx.kg  # noqa: SLF001
        gw = ctx.gateway._inner if isinstance(ctx.gateway, BudgetedGateway) else ctx.gateway  # noqa: SLF001
        return replace(ctx, kg=GuardedKG(kg, self.spec),
                       gateway=BudgetedGateway(gw, self.spec) if gw is not None else None)

    def check_events(self, events: list[str]) -> None:
        from .events import declared

        for e in events:
            if e not in self.spec.emits or not declared(e):
                raise PolicyViolation(f"{self.spec.name} emitted {e!r}, which it does not declare (spec.emits) or "
                                      "which is not a declared event")

    @abstractmethod
    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult: ...

    async def propose(self, ctx: AgentContext, fact: Fact, result: AgentResult) -> str:
        if fact.kind not in self.spec.writes:
            raise PolicyViolation(f"{self.spec.name} may not write fact kind {fact.kind!r}")
        fact = fact.model_copy(update={"agent": self.spec.name, "agent_version": self.spec.version,
                                       "run_id": fact.run_id or ctx.run_id})
        fid = await ctx.kg.propose_fact(fact)
        result.facts.append(fid)
        return fid

    async def register(self, ctx: AgentContext, entity: Entity) -> str:
        return await ctx.kg.register_entity(entity)
