"""Saga compensation framework."""

from __future__ import annotations

import pytest

from offramp.cutover.saga import (
    ActivitySpec,
    CompensationKind,
    SagaTransaction,
    compensate,
)


@pytest.mark.asyncio
async def test_compensate_runs_in_reverse() -> None:
    log: list[str] = []

    async def make_compensator(name: str):
        async def _c(payload):
            log.append(name)
            return {"compensated": name}

        return _c

    saga = SagaTransaction(saga_id="s1")
    for name in ["create_lead", "create_task", "send_email"]:
        spec = ActivitySpec(
            name=name,
            compensation_kind=CompensationKind.UNDO,
            compensate=await make_compensator(name),
        )
        saga.record(spec, {}, {"id": name})

    out = await compensate(saga)
    assert out.fully_compensated is True
    assert out.paused_for_human is False
    assert log == ["send_email", "create_task", "create_lead"]


@pytest.mark.asyncio
async def test_log_only_activities_succeed_without_function() -> None:
    saga = SagaTransaction(saga_id="s2")
    saga.record(
        ActivitySpec(name="audit_log", compensation_kind=CompensationKind.LOG_ONLY),
        {},
        {},
    )
    out = await compensate(saga)
    assert out.fully_compensated is True
    assert out.results[0].succeeded is True


@pytest.mark.asyncio
async def test_requires_human_pauses_compensation() -> None:
    saga = SagaTransaction(saga_id="s3")
    saga.record(
        ActivitySpec(
            name="email_to_customer",
            compensation_kind=CompensationKind.REQUIRES_HUMAN,
        ),
        {},
        {},
    )
    saga.record(
        ActivitySpec(
            name="create_record",
            compensation_kind=CompensationKind.UNDO,
            compensate=lambda p: _coro({"deleted": True}),  # type: ignore[no-untyped-call]
        ),
        {},
        {},
    )
    out = await compensate(saga)
    # Reverse order: create_record undoes successfully, email_to_customer
    # pauses for sign-off.
    assert out.paused_for_human is True
    assert not out.fully_compensated
    # The first result is for create_record (compensated first since reverse).
    assert out.results[0].activity_name == "create_record"
    assert out.results[1].activity_name == "email_to_customer"


@pytest.mark.asyncio
async def test_compensation_failure_recorded_but_loop_continues() -> None:
    async def boom(_p):
        raise RuntimeError("network down")

    async def succeed(_p):
        return {}

    saga = SagaTransaction(saga_id="s4")
    saga.record(
        ActivitySpec(name="ok", compensation_kind=CompensationKind.UNDO, compensate=succeed),
        {},
        {},
    )
    saga.record(
        ActivitySpec(name="bad", compensation_kind=CompensationKind.UNDO, compensate=boom),
        {},
        {},
    )
    out = await compensate(saga)
    assert not out.fully_compensated
    # Both are recorded; the failure didn't abort the loop.
    assert {r.activity_name for r in out.results} == {"ok", "bad"}
    assert any(r.succeeded is False for r in out.results)


@pytest.mark.asyncio
async def test_has_irreversible_actions() -> None:
    saga = SagaTransaction(saga_id="s5")
    saga.record(
        ActivitySpec(name="undo_able", compensation_kind=CompensationKind.UNDO),
        {},
        {},
    )
    assert not saga.has_irreversible_actions()
    saga.record(
        ActivitySpec(name="email_send", compensation_kind=CompensationKind.REQUIRES_HUMAN),
        {},
        {},
    )
    assert saga.has_irreversible_actions()


# Tiny coroutine helper so the lambda above works.
async def _coro(value):
    return value
