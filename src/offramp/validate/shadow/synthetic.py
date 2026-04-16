"""Synthetic CDC event source for tests + offline development.

Mirrors the wire shape the real Pub/Sub subscriber produces (Avro encode →
Avro decode round-trip), so tests exercise the same decoder path. Useful
for:

* unit/integration tests that don't have a real Salesforce org
* development loops where API quota is precious
* fault-injection (gap events, replay-id resets, lag simulation)
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from offramp.validate.shadow.avro_codec import SchemaCache, decode, encode
from offramp.validate.shadow.cdc_event import CDCEvent, ChangeEventHeader, ChangeType, now_utc
from offramp.validate.shadow.cdc_schema import build_change_event_schema, topic_for


@dataclass
class _ScheduledEvent:
    topic: str
    schema_id: str
    payload: dict[str, Any]
    header: ChangeEventHeader


@dataclass
class SyntheticSource:
    """Test-friendly CDC source.

    Use ``add_create``/``add_update``/``add_delete``/``add_gap`` to queue
    events; ``stream()`` yields them in order, then awaits more events with
    a configurable idle timeout. ``close()`` makes ``stream()`` return.
    """

    schema_cache: SchemaCache = field(default_factory=SchemaCache)
    schema_ids_by_entity: dict[str, str] = field(default_factory=dict)
    _queue: asyncio.Queue[CDCEvent | None] = field(default_factory=lambda: asyncio.Queue())
    _replay_counter: int = 0
    _seq: int = 0
    _latest_replay_id: str | None = None
    idle_timeout_s: float = 0.5

    @property
    def latest_replay_id(self) -> str | None:
        return self._latest_replay_id

    def register_entity(self, entity_name: str, fields: dict[str, str]) -> str:
        """Register an Avro schema for ``entity_name``; returns schema_id."""
        schema_id, schema = build_change_event_schema(entity_name=entity_name, fields=fields)
        self.schema_cache.register(schema_id, _to_json(schema))
        self.schema_ids_by_entity[entity_name] = schema_id
        return schema_id

    def _next_replay_id(self) -> str:
        self._replay_counter += 1
        return f"{self._replay_counter:020d}"

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build_event(
        self,
        *,
        entity_name: str,
        record_id: str,
        change_type: ChangeType,
        fields: dict[str, Any],
        changed_field_names: list[str] | None = None,
    ) -> CDCEvent:
        schema_id = self.schema_ids_by_entity[entity_name]
        schema = self.schema_cache.get(schema_id)
        replay_id = self._next_replay_id()
        header = ChangeEventHeader(
            entity_name=entity_name,
            change_type=change_type,
            change_origin="com/salesforce/api/soap/66.0;client=offramp_test",
            transaction_key=secrets.token_hex(8),
            sequence_number=self._next_seq(),
            commit_timestamp=int(now_utc().timestamp() * 1000),
            commit_user="005000000000000",
            commit_number=self._seq,
            record_ids=(record_id,),
            changed_fields=tuple(changed_field_names or fields.keys()),
        )
        # For gap events the payload is empty (only header present) — match
        # what the real Salesforce wire emits.
        if change_type.value.startswith("GAP_"):
            payload_fields = {k: None for k in fields}
        else:
            payload_fields = dict(fields)
        envelope = {
            "ChangeEventHeader": _header_to_avro(header),
            **payload_fields,
        }
        # Round-trip through Avro so the test exercises the real codec.
        encoded = encode(schema, envelope)
        decoded = decode(schema, encoded)
        out_fields = {k: v for k, v in decoded.items() if k != "ChangeEventHeader"}
        ev = CDCEvent(
            replay_id=replay_id,
            topic=topic_for(entity_name),
            schema_id=schema_id,
            received_at=now_utc(),
            header=header,
            fields=out_fields,
        )
        self._latest_replay_id = replay_id
        return ev

    def add_create(self, entity_name: str, record_id: str, fields: dict[str, Any]) -> CDCEvent:
        ev = self._build_event(
            entity_name=entity_name,
            record_id=record_id,
            change_type=ChangeType.CREATE,
            fields=fields,
        )
        self._queue.put_nowait(ev)
        return ev

    def add_update(
        self,
        entity_name: str,
        record_id: str,
        fields: dict[str, Any],
        changed: list[str] | None = None,
    ) -> CDCEvent:
        ev = self._build_event(
            entity_name=entity_name,
            record_id=record_id,
            change_type=ChangeType.UPDATE,
            fields=fields,
            changed_field_names=changed,
        )
        self._queue.put_nowait(ev)
        return ev

    def add_delete(self, entity_name: str, record_id: str) -> CDCEvent:
        ev = self._build_event(
            entity_name=entity_name,
            record_id=record_id,
            change_type=ChangeType.DELETE,
            fields={},
        )
        self._queue.put_nowait(ev)
        return ev

    def add_gap(
        self,
        entity_name: str,
        record_id: str,
        change_type: ChangeType = ChangeType.GAP_UPDATE,
    ) -> CDCEvent:
        if not change_type.value.startswith("GAP_"):
            raise ValueError(f"add_gap requires a GAP_* change_type, got {change_type}")
        # Mimic the wire: gap events carry only header data — no field values.
        # Use an empty fields map at the call site; the schema's null defaults
        # handle the missing values during decode.
        schema_id = self.schema_ids_by_entity[entity_name]
        schema = self.schema_cache.get(schema_id)
        # Field set comes from the schema (every non-header field, defaulted null).
        field_names = [f["name"] for f in schema["fields"] if f["name"] != "ChangeEventHeader"]
        ev = self._build_event(
            entity_name=entity_name,
            record_id=record_id,
            change_type=change_type,
            fields=dict.fromkeys(field_names),
        )
        self._queue.put_nowait(ev)
        return ev

    async def stream(self, topics: list[str]) -> AsyncIterator[CDCEvent]:
        wanted = set(topics)
        while True:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=self.idle_timeout_s)
            except TimeoutError:
                return
            if ev is None:  # close signal
                return
            if not wanted or ev.topic in wanted:
                yield ev

    async def close(self) -> None:
        await self._queue.put(None)


def _to_json(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)


def _header_to_avro(h: ChangeEventHeader) -> dict[str, Any]:
    return {
        "entityName": h.entity_name,
        "recordIds": list(h.record_ids),
        "changeType": h.change_type.value,
        "changeOrigin": h.change_origin,
        "transactionKey": h.transaction_key,
        "sequenceNumber": h.sequence_number,
        "commitTimestamp": h.commit_timestamp,
        "commitNumber": h.commit_number,
        "commitUser": h.commit_user,
        "changedFields": list(h.changed_fields),
        "diffFields": list(h.diff_fields),
        "nulledFields": list(h.nulled_fields),
    }
