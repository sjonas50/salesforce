"""OoE runtime — cascade depth + mixed-DML detection (AD-23).

The mixed-DML cases here are the explicit AD-23 coverage called out in
docs/architecture.md §7.
"""

from __future__ import annotations

import pytest

from offramp.runtime.ooe.state_machine import (
    CascadeDepthExceededError,
    MixedDMLError,
    OoERuntime,
    TransactionContext,
)
from offramp.runtime.rules.engine import RulesEngine

pytestmark = pytest.mark.ooe


def _empty_runtime(**kwargs) -> OoERuntime:
    return OoERuntime(rules=RulesEngine(), **kwargs)


def test_setup_then_nonsetup_in_same_txn_raises_mixed_dml() -> None:
    rt = _empty_runtime()
    ctx = TransactionContext(
        transaction_id="t1",
        triggering_record={},
        sobject="User",
    )
    rt.execute_save(sobject="User", record={"Username": "u@x"}, parent_ctx=ctx)
    with pytest.raises(MixedDMLError):
        rt.execute_save(sobject="Account", record={"Name": "A"}, parent_ctx=ctx)


def test_nonsetup_then_setup_also_raises_mixed_dml() -> None:
    rt = _empty_runtime()
    ctx = TransactionContext(
        transaction_id="t2",
        triggering_record={},
        sobject="Account",
    )
    rt.execute_save(sobject="Account", record={"Name": "A"}, parent_ctx=ctx)
    with pytest.raises(MixedDMLError):
        rt.execute_save(sobject="User", record={"Username": "u@x"}, parent_ctx=ctx)


def test_two_nonsetup_objects_are_fine() -> None:
    rt = _empty_runtime()
    ctx = TransactionContext(
        transaction_id="t3",
        triggering_record={},
        sobject="Account",
    )
    rt.execute_save(sobject="Account", record={"Name": "A"}, parent_ctx=ctx)
    rt.execute_save(sobject="Lead", record={"Email": "x@y"}, parent_ctx=ctx)
    assert ctx.cascade_depth == 2  # incremented once per cascaded child


def test_cascade_depth_limit_raises() -> None:
    rt = _empty_runtime(cascade_depth_limit=3)
    ctx = TransactionContext(
        transaction_id="t4",
        triggering_record={},
        sobject="Account",
    )
    # Each save with parent_ctx increments depth — 4th cascaded save trips.
    rt.execute_save(sobject="Account", record={"Id": "001"}, parent_ctx=ctx)
    rt.execute_save(sobject="Account", record={"Id": "002"}, parent_ctx=ctx)
    rt.execute_save(sobject="Account", record={"Id": "003"}, parent_ctx=ctx)
    with pytest.raises(CascadeDepthExceededError):
        rt.execute_save(sobject="Account", record={"Id": "004"}, parent_ctx=ctx)


def test_setup_object_case_insensitive() -> None:
    rt = _empty_runtime()
    ctx = TransactionContext(
        transaction_id="t5",
        triggering_record={},
        sobject="user",  # lowercase
    )
    rt.execute_save(sobject="user", record={}, parent_ctx=ctx)
    with pytest.raises(MixedDMLError):
        rt.execute_save(sobject="Account", record={}, parent_ctx=ctx)
