"""Drive a flow in the org with a debug trace on and return the log text.

Record-triggered flows are exercised with a *recipe*: the record to insert (and
optionally the update to apply), which the runner creates, updates, and deletes
again. Autolaunched flows are invoked through the Flow REST action. Screen flows
cannot be driven headlessly and are reported as not verifiable.

Every call goes through the MCP gateway (quota, anchoring); the debug level and
trace flag are created for the traced user and removed afterwards.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.process import ProcessDefinition, TriggerKind

log = get_logger(__name__)

DEBUG_LEVEL_NAME = "OfframpVerify"


@dataclass
class Recipe:
    """How to make one flow fire."""

    object: str | None = None
    create: dict[str, Any] = field(default_factory=dict)
    update: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)  # autolaunched flow inputs

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Recipe:
        return cls(
            object=d.get("object"),
            create=dict(d.get("create", {})),
            update=dict(d.get("update", {})),
            inputs=dict(d.get("inputs", {})),
        )


@dataclass
class RunOutcome:
    log_text: str
    record_id: str | None = None
    invoked: bool = False
    note: str = ""


async def _ensure_debug_level(gateway: Any) -> str:
    rows = (
        await gateway.sf_tooling_query(
            f"SELECT Id FROM DebugLevel WHERE DeveloperName = '{DEBUG_LEVEL_NAME}'"
        )
    ).get("records", [])
    if rows:
        return str(rows[0]["Id"])
    created = await gateway.sf_request(
        "POST",
        "tooling/sobjects/DebugLevel",
        {
            "DeveloperName": DEBUG_LEVEL_NAME,
            "MasterLabel": DEBUG_LEVEL_NAME,
            "Workflow": "FINER",
            "ApexCode": "FINE",
            "Database": "INFO",
            "Validation": "INFO",
            "Callout": "NONE",
            "System": "NONE",
            "Visualforce": "NONE",
            "ApexProfiling": "NONE",
            "Wave": "NONE",
            "Nba": "NONE",
        },
    )
    return str(created["id"])


async def _trace_flag(gateway: Any, user_id: str, debug_level_id: str, minutes: int = 10) -> str:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    created = await gateway.sf_request(
        "POST",
        "tooling/sobjects/TraceFlag",
        {
            "TracedEntityId": user_id,
            "DebugLevelId": debug_level_id,
            "LogType": "USER_DEBUG",
            "StartDate": now.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "ExpirationDate": (now + timedelta(minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%S.000+0000"
            ),
        },
    )
    return str(created["id"])


async def _latest_logs(gateway: Any, user_id: str, since_iso: str, limit: int = 5) -> str:
    rows = (
        await gateway.sf_query(
            "SELECT Id FROM ApexLog WHERE LogUserId = '"
            f"{user_id}' AND StartTime >= {since_iso} ORDER BY StartTime DESC LIMIT {limit}"
        )
    ).get("records", [])
    bodies = []
    for r in reversed(rows):
        body = await gateway.sf_request("GET", f"sobjects/ApexLog/{r['Id']}/Body")
        bodies.append(body if isinstance(body, str) else str(body))
    return "\n".join(bodies)


async def run_with_trace(
    gateway: Any,
    process: ProcessDefinition,
    recipe: Recipe,
    *,
    user_id: str,
    settle_seconds: float = 2.0,
) -> RunOutcome:
    """Fire the flow once under a trace flag; clean up; return the debug log text."""
    from datetime import UTC, datetime

    kind = process.trigger.kind
    if kind is TriggerKind.SCREEN:
        return RunOutcome("", note="screen flows need a user; not verifiable headlessly")
    if kind is TriggerKind.RECORD_SAVE and not recipe.create:
        return RunOutcome("", note="record-triggered flow without a recipe (no record to save)")
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    dl = await _ensure_debug_level(gateway)
    tf = await _trace_flag(gateway, user_id, dl)
    record_id: str | None = None
    invoked = False
    try:
        if kind is TriggerKind.RECORD_SAVE:
            obj = recipe.object or process.trigger.object or ""
            res = await gateway.sf_create(obj, recipe.create)
            record_id = str(res.get("id"))
            if recipe.update:
                await gateway.sf_update(obj, record_id, recipe.update)
        elif kind in {TriggerKind.INVOCATION, TriggerKind.SCHEDULED}:
            await gateway.sf_request(
                "POST", f"actions/custom/flow/{process.name}", {"inputs": [recipe.inputs or {}]}
            )
            invoked = True
        else:
            return RunOutcome("", note=f"trigger kind {kind.value} is not driven by the runner")
        await asyncio.sleep(settle_seconds)
        text = await _latest_logs(gateway, user_id, started)
    finally:
        try:
            await gateway.sf_request("DELETE", f"tooling/sobjects/TraceFlag/{tf}")
        except Exception as exc:  # pragma: no cover - cleanup best effort
            log.warning("verify.traceflag_cleanup_failed", error=str(exc)[:160])
        if record_id and recipe.create:
            try:
                await gateway.sf_delete(recipe.object or process.trigger.object or "", record_id)
            except Exception as exc:  # pragma: no cover
                log.warning(
                    "verify.record_cleanup_failed", record_id=record_id, error=str(exc)[:160]
                )
    log.info("verify.run.done", process=process.name, invoked=invoked, log_bytes=len(text))
    return RunOutcome(text, record_id=record_id, invoked=invoked)


def flow_deploy_zip(api_name: str, flow_xml: str) -> bytes:
    """A single-flow Metadata API deploy package (``unpackaged/``-less, single package)."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "package.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
            f"<types><members>{api_name}</members><name>Flow</name></types>"
            "<version>66.0</version></Package>",
        )
        zf.writestr(f"flows/{api_name}.flow", flow_xml)
    return buf.getvalue()


async def deploy_roundtrip_copy(
    gateway: Any, process: ProcessDefinition, *, suffix: str = "_rt"
) -> dict[str, Any]:
    """Render ``process`` to Flow XML and deploy it as ``<name><suffix>`` (inactive copy)."""
    from offramp.knowledge.flow_xml import to_flow_xml

    name = f"{process.name}{suffix}"
    # Active so it fires on the same DML as the original; its own label so the two
    # interviews can be told apart in one debug log.
    xml = to_flow_xml(
        process, api_name=name, active=True, label=f"{process.label or process.name} (rt)"
    )
    result = await gateway.sf_mdapi_deploy(flow_deploy_zip(name, xml))
    log.info(
        "verify.roundtrip.deployed", process=process.name, copy=name, status=result.get("status")
    )
    return {"copy": name, **result}
