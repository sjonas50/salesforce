"""Field diff + 7-category divergence classification."""

from __future__ import annotations

from offramp.core.models import DivergenceCategory
from offramp.validate.shadow.categorize import categorize
from offramp.validate.shadow.cdc_event import (
    CDCEvent,
    ChangeEventHeader,
    ChangeType,
    now_utc,
)
from offramp.validate.shadow.diff import field_diff


def _ev(change_type: ChangeType = ChangeType.UPDATE) -> CDCEvent:
    return CDCEvent(
        replay_id="0001",
        topic="/data/AccountChangeEvent",
        schema_id="abc",
        received_at=now_utc(),
        header=ChangeEventHeader(
            entity_name="Account",
            change_type=change_type,
            change_origin="x",
            transaction_key="t",
            sequence_number=1,
            commit_timestamp=0,
            commit_user="u",
            commit_number=1,
            record_ids=("001",),
        ),
        fields={"Name": "Acme"},
    )


def test_diff_normalizes_blank_and_none() -> None:
    diffs = field_diff({"Name": "", "Industry": "Tech"}, {"Industry": "Tech"})
    # Empty string vs missing -> both normalize to None -> no diff.
    assert diffs == {}


def test_diff_reports_real_change() -> None:
    diffs = field_diff({"Status": "Approved"}, {"Status": "Pending"})
    assert diffs == {"Status": ("Approved", "Pending")}


def test_gap_event_routes_to_ad22_category() -> None:
    res = categorize(event=_ev(ChangeType.GAP_UPDATE), field_diffs={}, trace={})
    assert res.diverged
    assert res.category is DivergenceCategory.GAP_EVENT_FULL_REFETCH_REQUIRED


def test_no_diff_no_abort_means_clean() -> None:
    res = categorize(event=_ev(), field_diffs={}, trace={})
    assert not res.diverged
    assert res.severity == 0


def test_validation_abort_in_runtime_translates_to_translation_error() -> None:
    res = categorize(
        event=_ev(),
        field_diffs={},
        trace={"aborted": True, "abort_reason": "validation failed: Account.HasName"},
    )
    assert res.category is DivergenceCategory.TRANSLATION_ERROR


def test_mixed_dml_in_runtime_categorized_as_ooe() -> None:
    res = categorize(
        event=_ev(),
        field_diffs={},
        trace={"aborted": True, "abort_reason": "MixedDMLError: setup + non-setup"},
    )
    assert res.category is DivergenceCategory.OOE_ORDERING_MISMATCH


def test_numeric_only_diff_categorized_as_formula_edge() -> None:
    res = categorize(
        event=_ev(),
        field_diffs={"Amount": (100.0, 100.01)},
        trace={},
    )
    assert res.category is DivergenceCategory.FORMULA_EDGE_CASE


def test_governor_limit_avoided_categorized() -> None:
    res = categorize(
        event=_ev(),
        field_diffs={"Status": ("Open", "Closed")},
        trace={"governor_limit_avoided": True},
    )
    assert res.category is DivergenceCategory.GOVERNOR_LIMIT_BEHAVIOR


def test_test_env_artifact_categorized_low_severity() -> None:
    res = categorize(
        event=_ev(),
        field_diffs={"Owner": ("u1", "u2")},
        trace={"test_env_artifact": True, "test_env_artifact_reason": "different running user"},
    )
    assert res.category is DivergenceCategory.TEST_ENVIRONMENT_ARTIFACT
    assert res.severity <= 20


def test_fallback_is_translation_error() -> None:
    res = categorize(event=_ev(), field_diffs={"Status": ("A", "B")}, trace={})
    assert res.category is DivergenceCategory.TRANSLATION_ERROR
