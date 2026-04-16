"""Phase 0.10: MCP gateway anchors every Salesforce call."""

from __future__ import annotations

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway


@pytest.mark.asyncio
async def test_create_anchors_payload() -> None:
    engram = InMemoryEngramClient()
    backend = InMemorySalesforceBackend()
    gw = MCPGateway(backend=backend, engram=engram)

    res = await gw.sf_create("Account", {"Name": "Acme"})

    assert res["success"]
    matches = await engram.find_by_hash(
        # Single anchor for the create call; look it up by component.
        next(iter(engram._by_hash.keys()))
    )
    assert len(matches) == 1
    assert matches[0].component == "mcp.gateway"
    assert matches[0].payload["tool"] == "sf_create"
    assert matches[0].payload["sobject"] == "Account"


@pytest.mark.asyncio
async def test_query_returns_records_inserted_by_create() -> None:
    engram = InMemoryEngramClient()
    backend = InMemorySalesforceBackend()
    gw = MCPGateway(backend=backend, engram=engram)

    await gw.sf_create("Account", {"Name": "Acme"})
    await gw.sf_create("Account", {"Name": "Globex"})
    res = await gw.sf_query("SELECT Id, Name FROM Account")

    assert res["totalSize"] == 2
    names = sorted(r["Name"] for r in res["records"])
    assert names == ["Acme", "Globex"]


@pytest.mark.asyncio
async def test_update_then_query_reflects_change() -> None:
    engram = InMemoryEngramClient()
    backend = InMemorySalesforceBackend()
    gw = MCPGateway(backend=backend, engram=engram)

    create_res = await gw.sf_create("Lead", {"Status": "New"})
    lead_id = create_res["id"]

    await gw.sf_update("Lead", lead_id, {"Status": "Working"})
    res = await gw.sf_query("SELECT Id, Status FROM Lead")
    assert res["records"][0]["Status"] == "Working"


@pytest.mark.asyncio
async def test_delete_removes_record() -> None:
    engram = InMemoryEngramClient()
    backend = InMemorySalesforceBackend()
    gw = MCPGateway(backend=backend, engram=engram)

    create_res = await gw.sf_create("Contact", {"LastName": "Doe"})
    await gw.sf_delete("Contact", create_res["id"])
    res = await gw.sf_query("SELECT Id FROM Contact")
    assert res["totalSize"] == 0
