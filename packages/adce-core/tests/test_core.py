from typing import Any

import pytest
from adce_core.data import DremioSource, DuckDBSource, ReadOnlyViolation
from adce_core.facts import Evidence, Fact, FactStatus, Trust, Venue
from adce_core.ports import MemoryKG, PolicyError


def make_fact(**kw):
    base: dict[str, Any] = dict(
        kind="scope_link", subject="app:a/table:Orders", value={"source": "Dremio_Sales:sales.erp.orders_v2"},
        trust=Trust.DOCUMENTED, evidence=[Evidence(kind="script_span", ref="script.qvs:28-29")], agent="bi_extractor")
    base.update(kw)
    return Fact(**base)


async def test_fact_ids_are_stable_and_proposals_idempotent():
    kg = MemoryKG()
    a, b = make_fact(), make_fact()
    assert a.id == b.id
    assert await kg.propose_fact(a) == await kg.propose_fact(b)
    assert len(kg.log) == 1


async def test_agents_cannot_confirm_or_write_without_evidence():
    kg = MemoryKG()
    with pytest.raises(PolicyError):
        await kg.propose_fact(make_fact(trust=Trust.CONFIRMED))
    with pytest.raises(PolicyError):
        await kg.propose_fact(make_fact(evidence=[]))
    with pytest.raises(PolicyError):
        await kg.propose_fact(make_fact(status=FactStatus.CONFIRMED))


async def test_decision_supersedes_and_keeps_history():
    kg = MemoryKG()
    fid = await kg.propose_fact(make_fact(trust=Trust.INFERRED))
    new_id = await kg.decide(fid, FactStatus.CONFIRMED, reviewer="owner", correction={"note": "checked"})
    current = await kg.facts(kind="scope_link")
    assert [f.id for f in current] == [new_id] and new_id != fid
    assert current[0].trust == Trust.CONFIRMED and current[0].supersedes == fid
    assert current[0].value["note"] == "checked" and current[0].value["_decided_by"] == "owner"
    assert len(await kg.facts(kind="scope_link", current=False)) == 2
    assert [f.id for f in await kg.search("orders_v2")] == [new_id]


def test_duckdb_source_is_read_only_and_local():
    src = DuckDBSource()
    src.load_rows("orders", ["id", "amount"], [(1, 10.0), (2, 20.0)])
    r = src.run_query("SELECT COUNT(*) AS n, SUM(amount) AS s FROM orders", label="test")
    assert r.rows == [(2, 30.0)] and r.venue == Venue.LOCAL
    for bad in ("DROP TABLE orders", "SELECT 1; DROP TABLE orders", "INSERT INTO orders VALUES (3, 1)"):
        with pytest.raises(ReadOnlyViolation):
            src.run_query(bad, label="test")


def test_dremio_source_waits_for_phase_pd():
    with pytest.raises(NotImplementedError):
        DremioSource()
