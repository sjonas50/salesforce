"""Phase 0.8: Engram stub contract."""

from __future__ import annotations

import pytest

from offramp.engram.client import InMemoryEngramClient, open_client


@pytest.mark.asyncio
async def test_anchor_returns_record_with_hash() -> None:
    c = InMemoryEngramClient()
    record = await c.anchor("test", {"hello": "world"})
    assert record.component == "test"
    assert len(record.content_hash) == 64
    assert record.payload == {"hello": "world"}


@pytest.mark.asyncio
async def test_get_round_trip() -> None:
    c = InMemoryEngramClient()
    record = await c.anchor("test", {"k": 1})
    fetched = await c.get(record.anchor_id)
    assert fetched == record


@pytest.mark.asyncio
async def test_find_by_hash_groups_identical_payloads() -> None:
    c = InMemoryEngramClient()
    a = await c.anchor("comp_a", {"k": 1})
    b = await c.anchor("comp_b", {"k": 1})  # same payload, different component
    matches = await c.find_by_hash(a.content_hash)
    assert {m.anchor_id for m in matches} == {a.anchor_id, b.anchor_id}


@pytest.mark.asyncio
async def test_open_client_default_is_in_memory() -> None:
    async with open_client() as c:
        rec = await c.anchor("x", {"y": 2})
        assert rec.payload == {"y": 2}
