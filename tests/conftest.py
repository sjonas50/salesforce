"""Test fixtures shared across the suite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from offramp.engram.client import InMemoryEngramClient
from offramp.event_bus.in_memory import InMemoryEventBus
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on a developer's .env (auth mode, log format, org alias)."""
    monkeypatch.setenv("SF_AUTH_MODE", "jwt")
    monkeypatch.setenv("SF_ORG_ALIAS", "dev_scratch")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.delenv("OFFRAMP_LIBRARY", raising=False)


@pytest_asyncio.fixture
async def engram() -> AsyncIterator[InMemoryEngramClient]:
    yield InMemoryEngramClient()


@pytest.fixture
def in_memory_sf() -> InMemorySalesforceBackend:
    return InMemorySalesforceBackend()


@pytest_asyncio.fixture
async def gateway(
    in_memory_sf: InMemorySalesforceBackend,
    engram: InMemoryEngramClient,
) -> AsyncIterator[MCPGateway]:
    yield MCPGateway(backend=in_memory_sf, engram=engram)


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    yield InMemoryEventBus()
