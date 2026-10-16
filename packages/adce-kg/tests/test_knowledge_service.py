"""KnowledgeService against the real local stack: Postgres fact log + Graphiti on Neo4j."""
from typing import Any

import psycopg
import pytest
from adce_core.facts import Entity, Evidence, Fact, FactStatus, Trust
from adce_core.ports import PolicyError
from adce_kg.graphiti_projection import edge_uuid, node_uuid

from .conftest import TEST_DSN

pytestmark = pytest.mark.integration

APP = "app:sales_overview"
TABLE = f"{APP}/table:Orders"
SOURCE = "source:Dremio_Sales:sales.erp.orders_v2"


def scope_link(**kw: Any) -> Fact:
    base: dict[str, Any] = dict(
        kind="scope_link", subject=TABLE, object=SOURCE, value={"statement": 9, "via": "sql_select"},
        trust=Trust.DOCUMENTED, agent="bi_extractor", tenant="test",
        evidence=[Evidence(kind="script_span", ref="sales_overview/script.qvs:28-29")])
    base.update(kw)
    return Fact(**base)


async def test_register_propose_and_project(service):
    await service.register_entity(Entity(key=APP, type="BIApp", name="Sales Overview"))
    await service.register_entity(Entity(key=TABLE, type="QlikTable", name="Orders", attributes={"app": APP}))
    fid = await service.propose_fact(scope_link())
    assert await service.propose_fact(scope_link()) == fid  # idempotent

    facts = await service.facts(subject=TABLE)
    assert [f.id for f in facts] == [fid]

    # projected into Graphiti: labelled nodes, an edge with trust, and an evidence episode
    recs, _, _ = await service.graph.driver.execute_query(
        "MATCH (a:Entity {uuid: $a})-[e:RELATES_TO {uuid: $e}]->(b:Entity {uuid: $b}) "
        "RETURN labels(a) AS la, e.trust AS trust, e.name AS kind, size(e.episodes) AS eps, b.key AS bkey",
        a=node_uuid(TABLE, service.graph.group_id), e=edge_uuid(fid),
        b=node_uuid(SOURCE, service.graph.group_id))
    assert len(recs) == 1
    r = recs[0]
    assert "QlikTable" in r["la"] and r["trust"] == "documented" and r["kind"] == "scope_link"
    assert r["eps"] == 1 and r["bkey"] == SOURCE  # placeholder node for the not-yet-registered source
    assert service.log.pending() == []


async def test_decision_supersedes_in_log_and_graph(service):
    fid = scope_link().id
    new_id = await service.decide(fid, FactStatus.CONFIRMED, reviewer="report_owner")
    current = await service.facts(subject=TABLE)
    assert [f.id for f in current] == [new_id] and current[0].trust == Trust.CONFIRMED
    assert len(await service.facts(subject=TABLE, current=False)) == 2
    recs, _, _ = await service.graph.driver.execute_query(
        "MATCH ()-[e:RELATES_TO]->() WHERE e.uuid IN [$old, $new] "
        "RETURN e.uuid AS u, e.expired_at AS x, e.status AS s",
        old=edge_uuid(fid), new=edge_uuid(new_id))
    by = {r["u"]: r for r in recs}
    assert by[edge_uuid(fid)]["x"] is not None and by[edge_uuid(fid)]["s"] == "superseded"
    assert by[edge_uuid(new_id)]["x"] is None and by[edge_uuid(new_id)]["s"] == "confirmed"


async def test_search_and_context(service):
    hits = await service.search("orders_v2")
    assert hits and all(h.status == FactStatus.CONFIRMED for h in hits)  # superseded edge is not returned
    ctx = await service.context(TABLE)
    assert ctx["entity"]["name"] == "Orders"
    assert any(n["other"] == SOURCE for n in ctx["neighbourhood"])


async def test_fact_log_is_append_only_in_the_database(service):
    with psycopg.connect(TEST_DSN) as con, pytest.raises(psycopg.errors.RaiseException):
        con.execute("UPDATE fact_log SET trust = 'confirmed'")


async def test_policy_is_enforced(service):
    with pytest.raises(PolicyError):
        await service.propose_fact(scope_link(trust=Trust.CONFIRMED, value={"x": 1}))


async def test_projection_rebuilds_from_the_log(service):
    n = await service.rebuild_projection()
    assert n >= 4
    recs, _, _ = await service.graph.driver.execute_query(
        "MATCH ()-[e:RELATES_TO {group_id: $g}]->() RETURN count(e) AS c", g=service.graph.group_id)
    assert recs[0]["c"] == 2


async def test_tenants_are_isolated_in_the_log(service):
    """Another tenant on the same database sees none of this tenant's facts, entities or events, and a fact of one
    tenant is refused by the other's log."""
    from adce_kg.fact_log import PostgresFactLog

    other = PostgresFactLog(TEST_DSN, "other_client")
    try:
        assert other.query(kind=None, subject=TABLE, current=True) == []
        assert other.entity(TABLE) is None and other.events_after(0) == []
        with pytest.raises(ValueError, match="tenant"):
            other.append(scope_link(), "fact.proposed")  # a fact of tenant "test"
        assert other.append(scope_link(tenant="default"), "fact.proposed")  # bound to other_client on write
        assert [f.tenant for f in other.query(kind="scope_link", subject=TABLE, current=True)] == ["other_client"]
        assert all(f.tenant == "test" for f in await service.facts(subject=TABLE, current=False))
    finally:
        other.close()


def test_the_log_does_not_start_without_a_tenant():
    from adce_kg.fact_log import PostgresFactLog

    with pytest.raises(ValueError, match="tenant"):
        PostgresFactLog(TEST_DSN, "")


async def test_readonly_cypher_is_read_only_and_tenant_bound(service):
    rows = await service.readonly_cypher("MATCH (n:Entity) WHERE n.group_id = $group_id RETURN n.uuid AS u", {}, 5)
    assert rows and all("u" in r for r in rows)
    with pytest.raises(PolicyError, match="read queries"):
        await service.readonly_cypher("MATCH (n) WHERE n.group_id = $group_id DETACH DELETE n", {}, 5)
    with pytest.raises(PolicyError, match="group_id"):
        await service.readonly_cypher("MATCH (n) RETURN n LIMIT 1", {}, 5)
