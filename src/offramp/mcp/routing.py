"""Per-process routing config consumed by the MCP gateway.

Architecture §11.4: rollback is "one configuration change — set percentage
to 0 — that takes effect on the next request." That requires the gateway to
read its routing config from a place the orchestrator can flip atomically.

The :class:`RoutingTable` is in-memory + persisted to Postgres so a gateway
restart picks up the last known config and the orchestrator can update it
atomically. Reads are lock-free (immutable snapshot per get_config call);
writes take a small async lock.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg

from offramp.core.logging import get_logger
from offramp.cutover.router import RoutingConfig, Target, route_for_record

log = get_logger(__name__)


_ROUTING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routing_config (
    process_id        text PRIMARY KEY,
    stage_percent     int NOT NULL,
    hash_seed         text NOT NULL,
    entered_stage_at  timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass
class RoutingTable:
    """In-memory routing table backed by Postgres.

    Construct with the same Postgres DSN the shadow store uses (or a
    different one — they don't share data). Call :meth:`reload` after
    construction; subsequent reads are O(1) lookups.
    """

    dsn: str
    _pool: asyncpg.Pool | None = None
    _configs: dict[str, RoutingConfig] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                await conn.execute(_ROUTING_SCHEMA_SQL)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def reload(self) -> int:
        """Pull the current routing rows from Postgres into memory."""
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM routing_config")
        async with self._lock:
            self._configs = {
                r["process_id"]: RoutingConfig(
                    process_id=r["process_id"],
                    stage_percent=int(r["stage_percent"]),
                    hash_seed=r["hash_seed"],
                    entered_stage_at=_aware(r["entered_stage_at"]),
                )
                for r in rows
            }
        return len(self._configs)

    async def upsert(
        self,
        *,
        process_id: str,
        stage_percent: int,
        hash_seed: str,
        entered_stage_at: datetime | None = None,
    ) -> RoutingConfig:
        await self.connect()
        assert self._pool is not None
        when = entered_stage_at or datetime.now(UTC)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO routing_config (process_id, stage_percent, hash_seed, entered_stage_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (process_id)
                DO UPDATE SET stage_percent = EXCLUDED.stage_percent,
                              hash_seed = EXCLUDED.hash_seed,
                              entered_stage_at = EXCLUDED.entered_stage_at,
                              updated_at = now()
                """,
                process_id,
                stage_percent,
                hash_seed,
                when,
            )
        cfg = RoutingConfig(
            process_id=process_id,
            stage_percent=stage_percent,
            hash_seed=hash_seed,
            entered_stage_at=when,
        )
        async with self._lock:
            self._configs[process_id] = cfg
        log.info(
            "mcp.routing.upsert",
            process=process_id,
            stage=stage_percent,
        )
        return cfg

    async def instant_rollback(self, process_id: str) -> RoutingConfig | None:
        """One-shot: set the process to 0% — instant rollback (architecture §11.4)."""
        existing = await self.get_config(process_id)
        if existing is None:
            return None
        cfg = await self.upsert(
            process_id=process_id,
            stage_percent=0,
            hash_seed=existing.hash_seed,
        )
        log.warning("mcp.routing.instant_rollback", process=process_id)
        return cfg

    async def get_config(self, process_id: str) -> RoutingConfig | None:
        async with self._lock:
            return self._configs.get(process_id)

    async def route(self, process_id: str, record_id: str) -> Target:
        cfg = await self.get_config(process_id)
        if cfg is None:
            return "salesforce"  # safe default — no config = no migration yet
        return route_for_record(cfg, record_id)

    async def list_configs(self) -> list[RoutingConfig]:
        async with self._lock:
            return list(self._configs.values())

    async def export_snapshot(self) -> str:
        async with self._lock:
            return json.dumps(
                [
                    {
                        "process_id": c.process_id,
                        "stage_percent": c.stage_percent,
                        "hash_seed": c.hash_seed,
                        "entered_stage_at": c.entered_stage_at.isoformat(),
                    }
                    for c in self._configs.values()
                ],
                indent=2,
                sort_keys=True,
            )


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)
