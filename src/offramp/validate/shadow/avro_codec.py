"""Avro encode/decode for Pub/Sub event payloads.

Salesforce schemas are sent as JSON; this module caches the parsed schema
per ``schema_id`` and produces / consumes the byte-frame format the wire
expects (bare Avro values, NOT Object Container File framed).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import fastavro


@dataclass
class SchemaCache:
    """Holds parsed Avro schemas keyed by Pub/Sub schema_id."""

    _by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, schema_id: str, schema_json: str) -> dict[str, Any]:
        if schema_id not in self._by_id:
            self._by_id[schema_id] = json.loads(schema_json)
        return self._by_id[schema_id]

    def get(self, schema_id: str) -> dict[str, Any]:
        if schema_id not in self._by_id:
            raise KeyError(f"unknown schema_id: {schema_id}")
        return self._by_id[schema_id]


def encode(schema: dict[str, Any], record: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, record)
    return buf.getvalue()


def decode(schema: dict[str, Any], payload: bytes) -> dict[str, Any]:
    buf = io.BytesIO(payload)
    # fastavro.schemaless_reader requires writer_schema; reader_schema is
    # optional and defaults to identical-schema decoding.
    out = fastavro.schemaless_reader(buf, schema, schema)
    if not isinstance(out, dict):
        raise ValueError(f"expected dict from Avro decode, got {type(out).__name__}")
    return out
