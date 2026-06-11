"""Pluggable knowledge-graph backend (Neo4j default, FalkorDB retained).

The understanding layer writes the Component + Flow knowledge graph through a
single :class:`GraphBackend` surface — ``query(cypher, params)`` plus lifecycle
hooks. Both Neo4j and FalkorDB speak Cypher, so the loader's UNWIND/MERGE
queries are identical across backends; only connection + result marshalling
differ.

Result shape: every backend returns a :class:`QueryResult` whose ``result_set``
is a ``list[list]`` of positional row values — matching FalkorDB's native
``QueryResult.result_set`` so existing call sites and tests are unchanged.

Drivers are imported lazily inside each backend so this module (and the loader)
import cleanly in environments without the ``[graph]`` extra installed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from offramp.core.config import InfraSettings
from offramp.core.logging import get_logger

log = get_logger(__name__)


class QueryResult:
    """Uniform query result. ``result_set`` is a list of positional row lists."""

    __slots__ = ("result_set",)

    def __init__(self, result_set: list[list[Any]]) -> None:
        self.result_set = result_set

    @property
    def rows(self) -> list[list[Any]]:
        return self.result_set


@runtime_checkable
class GraphBackend(Protocol):
    """What the loader needs from a graph store."""

    name: str

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> QueryResult: ...

    def reset(self) -> None:
        """Drop this graph's contents so a re-load is idempotent."""
        ...

    def delete(self) -> None:
        """Destroy the graph entirely (test cleanup)."""
        ...

    def close(self) -> None:
        """Release connections/pools."""
        ...


class FalkorBackend:
    """FalkorDB (Redis-native, Cypher) backend.

    Wraps a FalkorDB ``Graph`` whose ``query()`` already returns a result with
    ``.result_set``, so this is a thin lifecycle adapter.
    """

    def __init__(self, *, url: str, name: str) -> None:
        from falkordb import FalkorDB  # lazy: only needed for this backend

        host, port = _split_redis_url(url)
        self.name = name
        self._client = FalkorDB(host=host, port=port)
        self._graph = self._client.select_graph(name)

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> QueryResult:
        res = self._graph.query(cypher, params=params or {})
        # FalkorDB's result already exposes .result_set (list[list]).
        return QueryResult(list(res.result_set))

    def reset(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._graph.delete()
        self._graph = self._client.select_graph(self.name)

    def delete(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._graph.delete()

    def close(self) -> None:  # FalkorDB client holds a redis pool; GC handles it.
        pass


class Neo4jBackend:
    """Neo4j (Bolt, Cypher) backend.

    Single-tenant per customer (AD-10): one Neo4j database holds one org's
    graph, so ``reset()`` wipes the configured database. For multiple orgs on
    one Neo4j Enterprise cluster, point ``neo4j_database`` at a per-org database.
    """

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str,
        name: str,
    ) -> None:
        from neo4j import GraphDatabase  # lazy: only needed for this backend

        self.name = name
        self._database = database
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    @classmethod
    def from_settings(cls, infra: InfraSettings, *, name: str) -> Neo4jBackend:
        return cls(
            uri=infra.neo4j_uri,
            user=infra.neo4j_user,
            password=infra.neo4j_password.get_secret_value(),
            database=infra.neo4j_database,
            name=name,
        )

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> QueryResult:
        with self._driver.session(database=self._database) as session:
            result = session.run(cypher, **(params or {}))
            rows = [list(record.values()) for record in result]
        return QueryResult(rows)

    def reset(self) -> None:
        # Wipe the database so a re-load is idempotent. Plain DETACH DELETE keeps
        # this inside one auto-commit transaction (portable; no CALL-IN-TX implicit
        # transaction caveat). Single-tenant graphs (AD-10) are well within heap.
        self.query("MATCH (n) DETACH DELETE n")

    def delete(self) -> None:
        self.reset()

    def close(self) -> None:
        self._driver.close()


def _split_redis_url(url: str) -> tuple[str, int]:
    """Parse host/port from a ``redis://host:port`` URL (FalkorDB transport)."""
    hostport = url.split("://", 1)[1] if "://" in url else url
    host, _, port_str = hostport.partition(":")
    return host or "localhost", int(port_str) if port_str else 6379
