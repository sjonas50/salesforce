"""Synthetic CDC source — Avro round-trip + gap event semantics."""

from __future__ import annotations

import pytest

from offramp.validate.shadow.cdc_event import ChangeType
from offramp.validate.shadow.synthetic import SyntheticSource


@pytest.mark.asyncio
async def test_create_event_round_trips_through_avro() -> None:
    src = SyntheticSource()
    src.register_entity("Account", {"Name": "string", "Industry": "string", "Amount": "double"})
    src.add_create("Account", "001ABC", {"Name": "Acme", "Industry": "Tech", "Amount": 1000.0})

    received = [ev async for ev in src.stream(topics=["/data/AccountChangeEvent"])]
    assert len(received) == 1
    ev = received[0]
    assert ev.header.entity_name == "Account"
    assert ev.header.change_type is ChangeType.CREATE
    # Avro round-trip preserved values + types.
    assert ev.fields["Name"] == "Acme"
    assert ev.fields["Industry"] == "Tech"
    assert ev.fields["Amount"] == 1000.0


@pytest.mark.asyncio
async def test_gap_event_is_flagged_and_payload_blank() -> None:
    src = SyntheticSource()
    src.register_entity("Account", {"Name": "string"})
    src.add_gap("Account", "001ABC")
    received = [ev async for ev in src.stream(topics=[])]
    assert len(received) == 1
    ev = received[0]
    assert ev.is_gap
    assert ev.fields == {"Name": None}


@pytest.mark.asyncio
async def test_replay_id_monotonic_across_events() -> None:
    src = SyntheticSource()
    src.register_entity("Lead", {"Email": "string"})
    src.add_create("Lead", "00Q1", {"Email": "a@x"})
    src.add_create("Lead", "00Q2", {"Email": "b@x"})
    src.add_create("Lead", "00Q3", {"Email": "c@x"})
    received = [ev async for ev in src.stream(topics=[])]
    ids = [ev.replay_id for ev in received]
    assert ids == sorted(ids)
    assert src.latest_replay_id == ids[-1]


@pytest.mark.asyncio
async def test_topic_filter_excludes_other_entities() -> None:
    src = SyntheticSource()
    src.register_entity("Account", {"Name": "string"})
    src.register_entity("Lead", {"Email": "string"})
    src.add_create("Account", "A1", {"Name": "Acme"})
    src.add_create("Lead", "L1", {"Email": "x@y"})
    received = [ev async for ev in src.stream(topics=["/data/AccountChangeEvent"])]
    assert [ev.header.entity_name for ev in received] == ["Account"]
