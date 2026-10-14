"""kg-mcp tools through a real MCP client (in-memory transport) over MemoryKG."""
import pytest
from adce_core.ports import MemoryKG
from fastmcp import Client
from fastmcp.exceptions import ToolError
from kg_mcp.server import create_server

FACT = {"kind": "reads_source", "subject": "app:a/table:Orders", "object": "source:Dremio_Sales:sales.erp.orders_v2",
        "value": {"via": "sql"}, "trust": "documented", "agent": "bi_extractor",
        "evidence": [{"kind": "script_span", "ref": "a/script.qvs:28-29"}]}


def structured(result) -> dict:
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture
def server():
    kg = MemoryKG()

    async def factory():
        return kg

    return create_server(factory)


async def test_tools_are_listed(server):
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"register_entity", "propose_fact", "get_entity", "get_facts", "search_context",
                     "propose_facts", "register_entities",
                     "get_context_pack", "find_similar_facts"}  # no decide: decisions come only from hitl-service


async def test_register_propose_read_search(server):
    async with Client(server) as c:
        await c.call_tool("register_entity", {"key": "app:a/table:Orders", "type": "QlikTable", "name": "Orders"})
        r = await c.call_tool("propose_fact", {"fact": FACT})
        fid = r.data["fact_id"]
        again = await c.call_tool("propose_fact", {"fact": FACT})
        assert again.data["fact_id"] == fid
        facts = structured(await c.call_tool("get_facts", {"subject": "app:a/table:Orders"}))
        assert [f["id"] for f in facts["result"]] == [fid]
        hits = structured(await c.call_tool("search_context", {"text": "orders_v2"}))["result"]
        assert hits[0]["id"] == fid
        pack = structured(await c.call_tool("get_context_pack", {"key": "app:a/table:Orders"}))
        assert pack["entity"]["name"] == "Orders" and len(pack["facts"]) == 1


async def test_policy_errors_reach_the_caller(server):
    async with Client(server) as c:
        with pytest.raises(ToolError, match="policy"):
            await c.call_tool("propose_fact", {"fact": {**FACT, "evidence": []}})
        with pytest.raises(ToolError):
            await c.call_tool("propose_fact", {"fact": {**FACT, "trust": "confirmed"}})
