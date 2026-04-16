"""Canonical CDC event shape used across the shadow pipeline.

Both the real Pub/Sub subscriber and the synthetic source produce these so
downstream code (executor, diff, categorizer) doesn't care which it came
from. Mirrors the Salesforce ChangeEventHeader shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ChangeType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    UNDELETE = "UNDELETE"
    GAP_CREATE = "GAP_CREATE"
    GAP_UPDATE = "GAP_UPDATE"
    GAP_DELETE = "GAP_DELETE"
    GAP_UNDELETE = "GAP_UNDELETE"
    GAP_OVERFLOW = "GAP_OVERFLOW"


@dataclass(frozen=True)
class ChangeEventHeader:
    """The header field every CDC event carries."""

    entity_name: str
    change_type: ChangeType
    change_origin: str
    transaction_key: str
    sequence_number: int
    commit_timestamp: int  # ms since epoch
    commit_user: str
    commit_number: int
    record_ids: tuple[str, ...]
    changed_fields: tuple[str, ...] = field(default_factory=tuple)
    diff_fields: tuple[str, ...] = field(default_factory=tuple)
    nulled_fields: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CDCEvent:
    """One decoded CDC event ready for the shadow executor."""

    replay_id: str  # hex-encoded bytes from the Pub/Sub stream
    topic: str  # e.g. /data/AccountChangeEvent
    schema_id: str
    received_at: datetime
    header: ChangeEventHeader
    fields: dict[str, Any]  # full record payload after applying changed_fields

    @property
    def is_gap(self) -> bool:
        return self.header.change_type.value.startswith("GAP_")

    @property
    def is_overflow(self) -> bool:
        return self.header.change_type is ChangeType.GAP_OVERFLOW


def now_utc() -> datetime:
    return datetime.now(UTC)
