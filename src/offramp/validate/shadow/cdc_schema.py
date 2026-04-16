"""Build Avro schemas matching the Salesforce CDC envelope.

The real Pub/Sub stream serves these dynamically per object; for synthetic
testing + offline development we build them here from a simple field-type
map. The shape mirrors what Salesforce ships so the same decoder code path
works against a live org.
"""

from __future__ import annotations

import json
from typing import Any

CHANGE_EVENT_HEADER_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "ChangeEventHeader",
    "namespace": "com.sforce.eventbus",
    "fields": [
        {"name": "entityName", "type": "string"},
        {"name": "recordIds", "type": {"type": "array", "items": "string"}},
        {
            "name": "changeType",
            "type": {
                "type": "enum",
                "name": "ChangeType",
                "symbols": [
                    "CREATE",
                    "UPDATE",
                    "DELETE",
                    "UNDELETE",
                    "GAP_CREATE",
                    "GAP_UPDATE",
                    "GAP_DELETE",
                    "GAP_UNDELETE",
                    "GAP_OVERFLOW",
                ],
            },
        },
        {"name": "changeOrigin", "type": "string", "default": ""},
        {"name": "transactionKey", "type": "string"},
        {"name": "sequenceNumber", "type": "int"},
        {"name": "commitTimestamp", "type": "long"},
        {"name": "commitNumber", "type": "long"},
        {"name": "commitUser", "type": "string"},
        {
            "name": "changedFields",
            "type": {"type": "array", "items": "string"},
            "default": [],
        },
        {
            "name": "diffFields",
            "type": {"type": "array", "items": "string"},
            "default": [],
        },
        {
            "name": "nulledFields",
            "type": {"type": "array", "items": "string"},
            "default": [],
        },
    ],
}


def build_change_event_schema(
    *,
    entity_name: str,
    fields: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """Build a CDC envelope schema for ``entity_name``.

    ``fields`` maps Salesforce field name → Avro primitive ("string", "int",
    "long", "boolean", "double"). All fields are nullable to match SF
    behavior (CDC partial-payload semantics).

    Returns ``(schema_id, schema_dict)``. ``schema_id`` is a stable hash so
    re-registrations of the same schema get the same id (matching the wire
    behavior).
    """
    record_fields: list[dict[str, Any]] = [
        {"name": "ChangeEventHeader", "type": CHANGE_EVENT_HEADER_SCHEMA},
    ]
    for fname, ftype in fields.items():
        record_fields.append({"name": fname, "type": ["null", ftype], "default": None})
    schema = {
        "type": "record",
        "name": f"{entity_name}ChangeEvent",
        "namespace": "com.sforce.eventbus",
        "fields": record_fields,
    }
    canonical = json.dumps(schema, sort_keys=True).encode("utf-8")
    import hashlib

    schema_id = hashlib.sha256(canonical).hexdigest()[:24]
    return schema_id, schema


def topic_for(entity_name: str) -> str:
    """SF CDC topic name convention: ``/data/<Object>ChangeEvent``."""
    return f"/data/{entity_name}ChangeEvent"
