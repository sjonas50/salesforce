"""Integration-style unit tests that the SOQL-injection defense fires
at the three known entry points BEFORE a hostile value reaches a SOQL
string. Uses a fake gateway that records any call — the defense is
verified by the fact that no query ever reaches the gateway on bad input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from offramp.validate.reconcile.resync import Resyncer
from offramp.validate.shadow.data_env import production_read_via_mcp
from offramp.validate.shadow.store import ShadowStore


@dataclass
class _RecordingGateway:
    """Records every sf_query call so we can assert nothing leaked."""

    calls: list[str] = field(default_factory=list)
    response_records: list[dict[str, Any]] = field(default_factory=list)

    async def sf_query(self, soql: str) -> dict[str, Any]:
        self.calls.append(soql)
        return {"records": list(self.response_records)}


class _NoopStore(ShadowStore):
    """In-memory stand-in for ShadowStore — avoids real Postgres in unit tests."""

    def __init__(self) -> None:  # no dsn / pool
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

    async def upsert_record(
        self,
        *,
        sobject: str,
        record_id: str,
        fields: dict[str, Any],
        replay_id: str | None,
    ) -> None:
        self._records[(sobject, record_id)] = fields

    async def delete_record(self, sobject: str, record_id: str) -> None:
        self._records.pop((sobject, record_id), None)


# -- resync_record -----------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_record_rejects_injected_sobject() -> None:
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store)

    out = await resyncer.resync_record(sobject="Account; DROP TABLE x", record_id="001000000000001")
    assert out["ok"] is False
    assert gw.calls == []  # nothing reached the gateway


@pytest.mark.asyncio
async def test_resync_record_rejects_injected_record_id() -> None:
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store)

    out = await resyncer.resync_record(sobject="Account", record_id="001' OR '1'='1")
    assert out["ok"] is False
    assert gw.calls == []


@pytest.mark.asyncio
async def test_resync_record_rejects_record_id_with_sql_comment() -> None:
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store)

    out = await resyncer.resync_record(sobject="Account", record_id="001000000000001--")
    assert out["ok"] is False
    assert gw.calls == []


# -- resync_batch ------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_batch_rejects_injected_sobject_whole_batch() -> None:
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store)

    ids = ["001000000000001", "002000000000002"]
    out = await resyncer.resync_batch(sobject="Account'--", record_ids=ids)
    assert all(r["ok"] is False for r in out)
    assert gw.calls == []


@pytest.mark.asyncio
async def test_resync_batch_rejects_single_bad_id_in_chunk() -> None:
    """One bad id spoils the chunk but other chunks still run."""
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store, batch_size=2)

    ids = [
        "001000000000001",
        "002000000000002",  # chunk 1 — valid
        "bad'id",
        "003000000000003",  # chunk 2 — contains bad id
    ]
    out = await resyncer.resync_batch(sobject="Account", record_ids=ids)
    # Chunk 1 reached the gateway (valid); chunk 2 rejected pre-query.
    assert len(gw.calls) == 1
    assert "'001000000000001','002000000000002'" in gw.calls[0]
    # bad'id + the id that followed it should both be reported as failed.
    assert sum(1 for r in out if r["ok"] is False) == 2


@pytest.mark.asyncio
async def test_resync_batch_with_all_valid_ids_builds_safe_query() -> None:
    gw = _RecordingGateway()
    store = _NoopStore()
    resyncer = Resyncer(gateway=gw, store=store)

    ids = ["001000000000001", "002000000000002", "003000000000003"]
    await resyncer.resync_batch(sobject="Account", record_ids=ids)
    assert len(gw.calls) == 1
    soql = gw.calls[0]
    # The generated SOQL is built from validated ids only.
    assert "Account" in soql
    assert "'001000000000001'" in soql
    # No injection fragments present.
    assert ";" not in soql
    assert "--" not in soql


# -- production_read_via_mcp -------------------------------------------------


@pytest.mark.asyncio
async def test_production_read_rejects_injected_sobject() -> None:
    gw = _RecordingGateway()
    reader = production_read_via_mcp(gw)
    result = await reader("Account; DROP TABLE x", "001000000000001")
    assert result is None  # silently drops, doesn't reach the gateway
    assert gw.calls == []


@pytest.mark.asyncio
async def test_production_read_rejects_injected_record_id() -> None:
    gw = _RecordingGateway()
    reader = production_read_via_mcp(gw)
    result = await reader("Account", "001' OR '1'='1")
    assert result is None
    assert gw.calls == []


@pytest.mark.asyncio
async def test_production_read_builds_safe_query_on_valid_input() -> None:
    gw = _RecordingGateway()
    reader = production_read_via_mcp(gw)
    await reader("Account", "001000000000001")
    assert len(gw.calls) == 1
    assert "Account" in gw.calls[0]
    assert "001000000000001" in gw.calls[0]
    assert ";" not in gw.calls[0]
