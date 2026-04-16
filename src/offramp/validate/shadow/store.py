"""Postgres-backed shadow store (asyncpg).

Schema (initialized on connect):

* ``shadow_record(sobject, record_id, fields_json, last_seen_replay_id, updated_at)``
  — the forked data environment's per-record state. Reads fall through to
  Salesforce when the record isn't here; writes intercepted by the shadow
  executor land here.
* ``divergence(id, process_id, replay_id, observed_at, diverged, category,
  field_diffs_json, trace_json, anchor_id)``
  — every shadow comparison observation (architecture §10.2).
* ``readiness_event(process_id, observed_at, replay_id, diverged, severity)``
  — append-only stream feeding the readiness scoring window.
* ``replay_state(process_id, latest_replay_id, last_event_at)``
  — checkpoint for AD-21 lag monitor.

Phase 4 ships the schema + a thin async DAO. SQL migrations are inline
since there's only one — when migrations grow, add Alembic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from offramp.core.logging import get_logger
from offramp.validate.shadow.cdc_event import CDCEvent

log = get_logger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shadow_record (
    sobject              text NOT NULL,
    record_id            text NOT NULL,
    fields_json          jsonb NOT NULL,
    last_seen_replay_id  text,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sobject, record_id)
);

CREATE TABLE IF NOT EXISTS divergence (
    id              bigserial PRIMARY KEY,
    process_id      text NOT NULL,
    replay_id       text NOT NULL,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    diverged        boolean NOT NULL,
    category        text,
    field_diffs     jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace           jsonb NOT NULL DEFAULT '{}'::jsonb,
    anchor_id       text
);
CREATE INDEX IF NOT EXISTS divergence_process_observed_at_idx
    ON divergence (process_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS readiness_event (
    id              bigserial PRIMARY KEY,
    process_id      text NOT NULL,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    replay_id       text NOT NULL,
    diverged        boolean NOT NULL,
    severity        smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS readiness_event_process_observed_at_idx
    ON readiness_event (process_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS replay_state (
    process_id              text PRIMARY KEY,
    latest_replay_id        text,
    last_event_at           timestamptz
);
"""


@dataclass
class ShadowStore:
    """Async DAO for the shadow Postgres database."""

    dsn: str
    _pool: asyncpg.Pool | None = None

    async def connect(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)
            await self.migrate()
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)

    async def reset(self) -> None:
        """Truncate all tables (test helper)."""
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute("TRUNCATE shadow_record, divergence, readiness_event, replay_state")

    # -- Forked record state ----------------------------------------------------

    async def upsert_record(
        self,
        *,
        sobject: str,
        record_id: str,
        fields: dict[str, Any],
        replay_id: str | None,
    ) -> None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shadow_record (sobject, record_id, fields_json, last_seen_replay_id)
                VALUES ($1, $2, $3::jsonb, $4)
                ON CONFLICT (sobject, record_id)
                DO UPDATE SET fields_json = EXCLUDED.fields_json,
                              last_seen_replay_id = EXCLUDED.last_seen_replay_id,
                              updated_at = now()
                """,
                sobject,
                record_id,
                json.dumps(fields),
                replay_id,
            )

    async def get_record(self, sobject: str, record_id: str) -> dict[str, Any] | None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT fields_json FROM shadow_record WHERE sobject=$1 AND record_id=$2",
                sobject,
                record_id,
            )
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row["fields_json"])
        return result

    async def delete_record(self, sobject: str, record_id: str) -> None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM shadow_record WHERE sobject=$1 AND record_id=$2",
                sobject,
                record_id,
            )

    # -- Divergence observations ------------------------------------------------

    async def write_divergence(
        self,
        *,
        process_id: str,
        replay_id: str,
        diverged: bool,
        category: str | None,
        field_diffs: dict[str, Any],
        trace: dict[str, Any],
        anchor_id: str | None,
        severity: int = 0,
    ) -> int:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO divergence (process_id, replay_id, diverged, category,
                                        field_diffs, trace, anchor_id)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
                RETURNING id
                """,
                process_id,
                replay_id,
                diverged,
                category,
                json.dumps(field_diffs),
                json.dumps(trace),
                anchor_id,
            )
            await conn.execute(
                """
                INSERT INTO readiness_event (process_id, replay_id, diverged, severity)
                VALUES ($1, $2, $3, $4)
                """,
                process_id,
                replay_id,
                diverged,
                severity,
            )
        assert row is not None
        return int(row["id"])

    async def divergences_for(self, process_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, process_id, replay_id, observed_at, diverged, category,
                       field_diffs::text AS field_diffs, trace::text AS trace, anchor_id
                FROM divergence
                WHERE process_id=$1
                ORDER BY observed_at DESC
                LIMIT $2
                """,
                process_id,
                limit,
            )
        return [
            {
                "id": r["id"],
                "process_id": r["process_id"],
                "replay_id": r["replay_id"],
                "observed_at": r["observed_at"],
                "diverged": r["diverged"],
                "category": r["category"],
                "field_diffs": json.loads(r["field_diffs"]),
                "trace": json.loads(r["trace"]),
                "anchor_id": r["anchor_id"],
            }
            for r in rows
        ]

    # -- Readiness scoring window ----------------------------------------------

    async def readiness_window(self, process_id: str, *, since: datetime) -> list[dict[str, Any]]:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT observed_at, diverged, severity
                FROM readiness_event
                WHERE process_id=$1 AND observed_at >= $2
                ORDER BY observed_at ASC
                """,
                process_id,
                since,
            )
        return [
            {"observed_at": r["observed_at"], "diverged": r["diverged"], "severity": r["severity"]}
            for r in rows
        ]

    # -- Replay state -----------------------------------------------------------

    async def update_replay_state(self, *, process_id: str, replay_id: str) -> None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO replay_state (process_id, latest_replay_id, last_event_at)
                VALUES ($1, $2, now())
                ON CONFLICT (process_id)
                DO UPDATE SET latest_replay_id = EXCLUDED.latest_replay_id,
                              last_event_at    = EXCLUDED.last_event_at
                """,
                process_id,
                replay_id,
            )

    async def get_replay_state(self, process_id: str) -> dict[str, Any] | None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT latest_replay_id, last_event_at FROM replay_state WHERE process_id=$1",
                process_id,
            )
        if row is None:
            return None
        return {
            "latest_replay_id": row["latest_replay_id"],
            "last_event_at": row["last_event_at"],
        }


@asynccontextmanager
async def open_store(dsn: str) -> AsyncIterator[ShadowStore]:
    """Connect, yield, close."""
    store = ShadowStore(dsn=dsn)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def event_record_id(event: CDCEvent) -> str:
    """First record id from the CDC envelope (the common case)."""
    return event.header.record_ids[0] if event.header.record_ids else ""
