"""Real Salesforce Pub/Sub API gRPC subscriber.

Connects to ``api.pubsub.salesforce.com:7443`` over gRPC + TLS, authenticates
via the same OAuth tokens the MCP gateway uses, and yields decoded
:class:`CDCEvent` instances. The schema cache fetches each new schema_id on
demand and reuses it across events.

Phase 4 ships the real implementation; an end-to-end run against a live
Salesforce org happens once Phase 5 deploys the MCP gateway. The synthetic
source covers the same wire format for tests today.
"""

from __future__ import annotations

import asyncio
import logging as _stdlogging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import grpc

from offramp.core.logging import get_logger
from offramp.validate.shadow.avro_codec import SchemaCache, decode
from offramp.validate.shadow.cdc_event import CDCEvent, ChangeEventHeader, ChangeType, now_utc
from offramp.validate.shadow.pubsub_proto import pubsub_api_pb2 as pb
from offramp.validate.shadow.pubsub_proto import pubsub_api_pb2_grpc as pb_grpc

log = get_logger(__name__)
_stdlogging.getLogger("grpc").setLevel(_stdlogging.WARNING)


@dataclass
class PubSubSubscriber:
    """gRPC + Avro Pub/Sub API client.

    ``access_token`` + ``instance_url`` come from the MCP gateway's auth
    layer. ``tenant_id`` is the Salesforce org id (15- or 18-char).

    Use :meth:`stream` to consume CDC events; the subscriber drives flow
    control internally — periodically sends ``FetchRequest`` messages to
    request more events from the server.
    """

    access_token: str
    instance_url: str
    tenant_id: str
    endpoint: str = "api.pubsub.salesforce.com:7443"
    fetch_batch_size: int = 25
    schema_cache: SchemaCache = field(default_factory=SchemaCache)
    _channel: grpc.aio.Channel | None = None
    _stub: pb_grpc.PubSubStub | None = None
    _latest_replay_id: str | None = None

    @property
    def latest_replay_id(self) -> str | None:
        return self._latest_replay_id

    async def _ensure(self) -> pb_grpc.PubSubStub:
        if self._stub is not None:
            return self._stub
        creds = grpc.ssl_channel_credentials()
        self._channel = grpc.aio.secure_channel(self.endpoint, creds)
        self._stub = pb_grpc.PubSubStub(self._channel)
        return self._stub

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        return (
            ("accesstoken", self.access_token),
            ("instanceurl", self.instance_url),
            ("tenantid", self.tenant_id),
        )

    async def _ensure_schema(self, schema_id: str) -> dict:
        try:
            return self.schema_cache.get(schema_id)
        except KeyError:
            stub = await self._ensure()
            req = pb.SchemaRequest(schema_id=schema_id)
            info = await stub.GetSchema(req, metadata=self._metadata())
            return self.schema_cache.register(schema_id, info.schema_json)

    async def stream(
        self,
        topics: list[str],
        *,
        replay_preset: str = "LATEST",
        replay_id: bytes | None = None,
    ) -> AsyncIterator[CDCEvent]:
        """Subscribe to one or more CDC topics.

        ``replay_preset`` is ``LATEST`` | ``EARLIEST`` | ``CUSTOM``. When
        ``CUSTOM``, supply ``replay_id`` (raw bytes from a prior
        ``latest_replay_id``).
        """
        stub = await self._ensure()
        preset = getattr(pb.ReplayPreset, replay_preset)

        # Bidirectional stream: client periodically pushes FetchRequest, server
        # pushes FetchResponse with up to N events.
        req_q: asyncio.Queue[pb.FetchRequest] = asyncio.Queue()

        async def request_iter() -> AsyncIterator[pb.FetchRequest]:
            for topic in topics:
                yield pb.FetchRequest(
                    topic_name=topic,
                    replay_preset=preset,
                    replay_id=replay_id or b"",
                    num_requested=self.fetch_batch_size,
                )
            # Subsequent FetchRequests come from req_q as we ack events.
            while True:
                req = await req_q.get()
                yield req

        async for resp in stub.Subscribe(request_iter(), metadata=self._metadata()):
            self._latest_replay_id = resp.latest_replay_id.hex() if resp.latest_replay_id else None
            for ev in resp.events:
                schema = await self._ensure_schema(ev.event.schema_id)
                payload = decode(schema, ev.event.payload)
                cdc = _to_cdc_event(
                    topic=topics[0] if topics else "",
                    schema_id=ev.event.schema_id,
                    payload=payload,
                    replay_id=ev.replay_id.hex(),
                )
                yield cdc
            # Ask for another batch.
            await req_q.put(
                pb.FetchRequest(
                    topic_name=topics[0],
                    replay_preset=pb.ReplayPreset.CUSTOM,
                    replay_id=resp.latest_replay_id,
                    num_requested=self.fetch_batch_size,
                )
            )

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None


def _to_cdc_event(
    *,
    topic: str,
    schema_id: str,
    payload: dict,
    replay_id: str,
) -> CDCEvent:
    """Convert a decoded Avro payload into a CDCEvent."""
    raw_header = payload.get("ChangeEventHeader") or {}
    change_type_value = raw_header.get("changeType", "UPDATE")
    if hasattr(change_type_value, "name"):
        change_type_value = change_type_value.name
    header = ChangeEventHeader(
        entity_name=raw_header.get("entityName", ""),
        change_type=ChangeType(str(change_type_value)),
        change_origin=raw_header.get("changeOrigin", ""),
        transaction_key=raw_header.get("transactionKey", ""),
        sequence_number=int(raw_header.get("sequenceNumber", 0)),
        commit_timestamp=int(raw_header.get("commitTimestamp", 0)),
        commit_user=raw_header.get("commitUser", ""),
        commit_number=int(raw_header.get("commitNumber", 0)),
        record_ids=tuple(raw_header.get("recordIds", []) or []),
        changed_fields=tuple(raw_header.get("changedFields", []) or []),
        diff_fields=tuple(raw_header.get("diffFields", []) or []),
        nulled_fields=tuple(raw_header.get("nulledFields", []) or []),
    )
    fields = {k: v for k, v in payload.items() if k != "ChangeEventHeader"}
    return CDCEvent(
        replay_id=replay_id,
        topic=topic,
        schema_id=schema_id,
        received_at=now_utc(),
        header=header,
        fields=fields,
    )
