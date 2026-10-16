"""The knowledge service's write path (validator, trust gate) and its queries (neighbours, lineage, join paths,
token-budgeted context packs), on the in-memory store."""
import asyncio

from adce_core.facts import Evidence, Fact, FactStatus, Trust
from adce_core.ports import MemoryKG
from adce_core.queries import context_pack, gate, join_paths, lineage, neighbors


def _f(kind: str, subject: str, value: dict, *, obj: str | None = None, trust: Trust = Trust.INFERRED,
       confidence: float = 1.0) -> Fact:
    return Fact(kind=kind, subject=subject, object=obj, value=value, trust=trust, confidence=confidence, agent="t",
                evidence=[Evidence(kind="rule_check", ref="t")])


def test_a_proposal_that_contradicts_a_confirmed_fact_is_kept_and_flagged():
    async def go():
        kg = MemoryKG()
        first = await kg.propose_fact(_f("resolves_to", "source:c:x.y", {"dataset": "lake.x.y"}))
        await kg.decide(first, FactStatus.CONFIRMED, "owner")
        other = await kg.propose_fact(_f("resolves_to", "source:c:x.y", {"dataset": "lake.copy.y"}))
        conflicts = await kg.facts(kind="conflict")
        assert len(conflicts) == 1 and other in conflicts[0].value["facts"]
        assert "lake.copy.y" in conflicts[0].value["reason"] and "lake.x.y" in conflicts[0].value["reason"]
        assert [e["topic"] for e in await kg.events()][-1] == "conflict.flagged"
        same = await kg.propose_fact(_f("resolves_to", "source:c:x.y", {"dataset": "lake.x.y", "note": 1}))
        assert same and len(await kg.facts(kind="conflict")) == 1  # agreeing with the confirmed value: no conflict

    asyncio.run(go())


def test_the_trust_gate_routes_each_proposal_and_records_its_impact():
    async def go():
        kg = MemoryKG()
        await kg.propose_fact(_f("bi_table", "app:a/table:T", {"fields": [], "steps": 1}))
        await kg.propose_fact(_f("report_sql", "app:a/table:T", {"level": "V4", "venue": "local"},
                                 trust=Trust.VALIDATED))
        await kg.propose_fact(_f("sql_checked", "app:a/table:T", {"ok": True, "rules": []}, trust=Trust.VALIDATED,
                                 confidence=0.95))
        routes = {e["payload"]["fact"]["kind"]: e["payload"]["review"] for e in await kg.events()
                  if e["topic"] == "fact.proposed"}
        assert routes == {"bi_table": "queue", "report_sql": "gate", "sql_checked": "auto"}
        impact = {f.kind: f.impact for f in await kg.facts(subject="app:a/table:T")}
        assert impact == {"bi_table": 0, "report_sql": 1, "sql_checked": 2}

    asyncio.run(go())
    assert gate(_f("sql_checked", "x", {"ok": 1, "rules": []}, trust=Trust.VALIDATED), impact=10) == "queue"


def test_neighbors_lineage_and_join_paths():
    async def go():
        kg = MemoryKG()
        await kg.propose_fact(_f("reads_source", "app:a/table:A", {"statement": 1}, obj="source:c:s.t"))
        await kg.propose_fact(_f("lineage", "app:a/table:A", {"inputs": [{"namespace": "dremio://c", "name": "s.t"}],
                                                             "outputs": [{"namespace": "qlik://a", "name": "A"}]}))
        await kg.propose_fact(_f("lineage", "app:a/table:B", {"inputs": [{"namespace": "qlik://a", "name": "A"}],
                                                             "outputs": [{"namespace": "qlik://a", "name": "B"}]}))
        for x, y, k, t in [("A", "B", ["k"], Trust.DOCUMENTED), ("B", "C", ["j"], Trust.DOCUMENTED),
                           ("A", "C", ["m"], Trust.VALIDATED)]:
            await kg.propose_fact(_f("associates", f"app:a/table:{x}", {"keys": k}, obj=f"app:a/table:{y}", trust=t))
        assert {f.kind for f in await neighbors(kg, "source:c:s.t")} == {"reads_source"}
        up = await lineage(kg, "app:a/table:B", "upstream")
        assert [s["table"] for s in up] == ["app:a/table:B", "app:a/table:A"]
        down = await lineage(kg, "app:a/table:A", "downstream")
        assert [s["table"] for s in down] == ["app:a/table:A", "app:a/table:B"]
        paths = await join_paths(kg, "app:a/table:A", "app:a/table:C")
        assert [[h["keys"] for h in p] for p in paths] == [[["m"]]]  # the direct link is the shortest

    asyncio.run(go())


def test_a_context_pack_puts_trusted_facts_first_and_fits_the_budget():
    async def go():
        kg = MemoryKG()
        for i in range(30):
            await kg.propose_fact(_f("bi_issue", "app:a", {"code": "x", "message": "m" * 200, "i": i}))
        fid = await kg.propose_fact(_f("variable_value", "app:a/var:v", {"value": "1"}))
        await kg.decide(fid, FactStatus.CONFIRMED, "owner")
        pack = await context_pack(kg, "app:a", token_budget=400)
        assert pack["truncated"] and pack["tokens"] <= 400
        assert pack["facts"][0]["trust"] == "confirmed"
        assert pack == await context_pack(kg, "app:a", token_budget=400)  # deterministic

    asyncio.run(go())
