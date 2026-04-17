"""Hash-deterministic traffic router contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from offramp.cutover.router import (
    STAGE_PERCENTS,
    RoutingConfig,
    next_stage,
    previous_stage,
    route_for_record,
)


def _cfg(percent: int) -> RoutingConfig:
    return RoutingConfig(
        process_id="p1",
        stage_percent=percent,
        hash_seed="seed",
        entered_stage_at=datetime.now(UTC),
    )


def test_zero_percent_routes_everything_to_salesforce() -> None:
    cfg = _cfg(0)
    for i in range(100):
        assert route_for_record(cfg, f"rec{i}") == "salesforce"


def test_one_hundred_percent_routes_everything_to_runtime() -> None:
    cfg = _cfg(100)
    for i in range(100):
        assert route_for_record(cfg, f"rec{i}") == "runtime"


def test_routing_is_deterministic_per_record() -> None:
    cfg = _cfg(50)
    decisions = {f"rec{i}": route_for_record(cfg, f"rec{i}") for i in range(20)}
    # Re-evaluate; same answers.
    for rec, target in decisions.items():
        assert route_for_record(cfg, rec) == target


def test_50_percent_splits_roughly_evenly() -> None:
    cfg = _cfg(50)
    rt = sum(1 for i in range(1000) if route_for_record(cfg, f"r{i}") == "runtime")
    # Allow 5% slack on either side of 50%.
    assert 450 <= rt <= 550, f"expected ~500/1000, got {rt}"


def test_changing_seed_changes_routing() -> None:
    cfg_a = _cfg(50)
    cfg_b = RoutingConfig(
        process_id="p1",
        stage_percent=50,
        hash_seed="different_seed",
        entered_stage_at=datetime.now(UTC),
    )
    diffs = sum(
        1
        for i in range(200)
        if route_for_record(cfg_a, f"r{i}") != route_for_record(cfg_b, f"r{i}")
    )
    # Different seeds reshuffle the buckets — a meaningful number should differ.
    assert diffs > 20


def test_dwell_complete_with_zero_dwell() -> None:
    cfg = RoutingConfig(
        process_id="p1",
        stage_percent=0,
        hash_seed="s",
        entered_stage_at=datetime.now(UTC),
    )
    assert cfg.dwell_complete()


def test_dwell_remaining_at_1_percent_is_about_48h() -> None:
    cfg = RoutingConfig(
        process_id="p1",
        stage_percent=1,
        hash_seed="s",
        entered_stage_at=datetime.now(UTC),
    )
    assert cfg.dwell_remaining() <= timedelta(hours=48)
    assert cfg.dwell_remaining() > timedelta(hours=47, minutes=59)
    assert not cfg.dwell_complete()


def test_dwell_complete_after_advancing_clock() -> None:
    cfg = RoutingConfig(
        process_id="p1",
        stage_percent=1,
        hash_seed="s",
        entered_stage_at=datetime.now(UTC) - timedelta(hours=49),
    )
    assert cfg.dwell_complete()


def test_stage_progression() -> None:
    assert next_stage(0) == 1
    assert next_stage(1) == 5
    assert next_stage(5) == 25
    assert next_stage(25) == 50
    assert next_stage(50) == 100
    assert next_stage(100) is None


def test_stage_regression() -> None:
    assert previous_stage(0) == 0
    assert previous_stage(1) == 0
    assert previous_stage(5) == 1
    assert previous_stage(25) == 5
    assert previous_stage(50) == 25
    assert previous_stage(100) == 50


def test_known_stage_set() -> None:
    assert STAGE_PERCENTS == (0, 1, 5, 25, 50, 100)
