"""Phase 0.10 smoke test.

Per the v2.1 plan, the real Phase 0 smoke target is "retrieve one Flow from a
scratch org, parse it, load it into FalkorDB, round-trip through a placeholder
generator." That requires:

* a Salesforce Developer Edition scratch org,
* a Connected App with JWT bearer flow configured,
* a JWT private key on disk,
* a running FalkorDB instance.

None of those are present in the local sandbox. Phase 0 ships an *equivalent
shape* using the in-memory MCP backend, so the smoke target proves the
end-to-end path (CLI → MCP gateway → backend → Engram anchoring) without
depending on external services. Replace this with a real-org variant once
Phase 0.12 (terraform/helm) lands and the JWT cert (Phase 0.11 runbook) is
provisioned.
"""

from __future__ import annotations

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_smoke_extract_round_trip() -> None:
    engram = InMemoryEngramClient()
    backend = InMemorySalesforceBackend()
    # Pre-load a "Flow" record that mimics what a real metadata pull would surface.
    backend.records["Flow"] = {
        "01F000000000001": {
            "Id": "01F000000000001",
            "DeveloperName": "LeadRouting",
            "ProcessType": "AutoLaunchedFlow",
            "Status": "Active",
        }
    }
    gw = MCPGateway(backend=backend, engram=engram)

    # Step 1: the gateway can list the Flow.
    flows = await gw.sf_query("SELECT Id, DeveloperName FROM Flow")
    assert flows["totalSize"] == 1
    flow = flows["records"][0]
    assert flow["DeveloperName"] == "LeadRouting"

    # Step 2: every gateway call landed an Engram anchor.
    # (One anchor for the query.)
    assert len(engram._records) == 1

    # Step 3: re-anchoring the same payload yields the same content hash, which
    # is the property the real Engram backend depends on for de-duplication.
    rec_a = await engram.anchor("test", {"flow": "LeadRouting"})
    rec_b = await engram.anchor("test", {"flow": "LeadRouting"})
    assert rec_a.content_hash == rec_b.content_hash
