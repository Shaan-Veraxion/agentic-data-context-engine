"""Contracts enforced at run time: fact kinds and their values, agents' knowledge tools, model routes and budgets,
and declared events."""
import asyncio
from typing import Any

import pytest
from adce_core.agent import (
    AgentBudget,
    AgentContext,
    AgentKind,
    AgentResult,
    AgentSpec,
    BaseAgent,
    BudgetExceeded,
    PolicyViolation,
)
from adce_core.artifacts import LocalArtifactStore
from adce_core.events import declared, outbox_problems
from adce_core.facts import Evidence, Fact, Trust
from adce_core.ports import MemoryKG, PolicyError


def _fact(kind: str, value: dict) -> Fact:
    return Fact(kind=kind, subject="app:a", value=value, trust=Trust.INFERRED, agent="t",
                evidence=[Evidence(kind="rule_check", ref="t")])


def test_a_fact_must_be_of_a_declared_kind_and_carry_its_required_keys():
    kg = MemoryKG()
    with pytest.raises(PolicyError, match="not declared"):
        asyncio.run(kg.propose_fact(_fact("made_up", {})))
    with pytest.raises(PolicyError, match="report_sql needs venue"):
        asyncio.run(kg.propose_fact(_fact("report_sql", {"level": "V2"})))
    asyncio.run(kg.propose_fact(_fact("report_sql", {"level": "V2", "venue": "local", "extra": 1})))


class _Gateway:
    async def call(self, route: str, inputs: dict, output_model: Any, **kw: Any) -> Any:
        class R:
            cost_usd = 0.0

        return R()


class _Agent(BaseAgent):
    spec = AgentSpec(number=None, name="probe", version="1", kind=AgentKind.HYBRID, triggers=[],
                     emits=["sql.checked"], writes=["report_sql"], routes=["repair_sql"],
                     tools=["get_facts", "propose_fact"], budget=AgentBudget(max_calls=1))

    def __init__(self, act: str) -> None:
        self.act = act

    async def run(self, job: dict, ctx: AgentContext) -> AgentResult:
        r = AgentResult()
        assert ctx.gateway is not None
        if self.act == "read":
            await ctx.kg.facts()
        if self.act == "search":
            await ctx.kg.search("x")
        if self.act == "route":
            await ctx.gateway.call("diagnose_mismatch", {}, None)
        if self.act == "budget":
            await ctx.gateway.call("repair_sql", {}, None)
            await ctx.gateway.call("repair_sql", {}, None)
        if self.act == "event":
            r.events.append("pack.published")
        if self.act == "ok_event":
            r.events.append("sql.checked")
        return r


def _ctx(tmp_path) -> AgentContext:
    return AgentContext(kg=MemoryKG(), artifacts=LocalArtifactStore(tmp_path), run_id="r", app_id="a",
                        gateway=_Gateway())


@pytest.mark.parametrize(("act", "error", "match"), [
    ("search", PolicyViolation, "search_context"),
    ("route", PolicyViolation, "diagnose_mismatch"),
    ("budget", BudgetExceeded, "1 model calls"),
    ("event", PolicyViolation, "pack.published"),
])
def test_an_agent_is_held_to_its_spec(tmp_path, act, error, match):
    with pytest.raises(error, match=match):
        asyncio.run(_Agent(act)({}, _ctx(tmp_path)))


def test_what_the_spec_allows_works(tmp_path):
    for act in ("read", "ok_event"):
        asyncio.run(_Agent(act)({}, _ctx(tmp_path)))


def test_events_and_outbox_payloads_are_declared():
    assert declared("bi_ir.ready") and declared("fact.confirmed") and not declared("bi_ir.done")
    assert outbox_problems("fact.proposed", {"fact": {}, "fact_id": "x"}) == []
    assert outbox_problems("fact.proposed", {"fact": {}}) and outbox_problems("fact.renamed", {})
