"""Compare Mode debug-log parser."""

from __future__ import annotations

from offramp.validate.compare_mode.log_parser import LogEventKind, parse

SAMPLE_LOG = """\
13:00:00.000 (1000)|EXECUTION_STARTED
13:00:00.001 (1001)|USER_INFO|[EXTERNAL]|0050000000000ABC|english|EST|UTF-8
13:00:00.010 (10000)|CODE_UNIT_STARTED|[EXTERNAL]|TRIGGERS|trigger LeadDispatcher on Lead trigger event AfterInsert for [00Q1]
13:00:00.020 (20000)|DML_BEGIN|[123]|Op:Insert|Type:Lead|Rows:1
13:00:00.030 (30000)|VALIDATION_RULE|[Lead.HasEmail]|true|Name:Lead.HasEmail
13:00:00.040 (40000)|VALIDATION_FAIL|VALIDATION_FAIL|Name:Lead.HasEmail|Email is required.
13:00:00.050 (50000)|FLOW_START_INTERVIEW_BEGIN|01F0000000001|LeadRouting
13:00:00.090 (90000)|EXECUTION_FINISHED
"""


def test_parses_single_transaction() -> None:
    txns, stats = parse(SAMPLE_LOG)
    assert stats.transactions == 1
    assert stats.classified > 0
    txn = txns[0]
    assert txn.user_id == "0050000000000ABC"
    assert txn.dml_ops == [{"op": "Insert", "sobject": "Lead", "rows": "1"}]
    assert txn.validation_failures == ["Lead.HasEmail"]
    assert txn.flows_invoked == ["LeadRouting"]


def test_classification_counts() -> None:
    _, stats = parse(SAMPLE_LOG)
    # The 8-line sample has 8 classifiable lines.
    assert stats.classified == 8
    assert stats.unclassified == 0


def test_unknown_event_kind_counted_as_unclassified() -> None:
    log = (
        "13:00:00.000 (1000)|EXECUTION_STARTED\n"
        "13:00:00.001 (1001)|TOTALLY_UNKNOWN_EVENT|whatever\n"
        "13:00:00.002 (1002)|EXECUTION_FINISHED\n"
    )
    _, stats = parse(log)
    assert stats.unclassified == 1
    assert stats.classified == 2


def test_lines_without_timestamp_skipped() -> None:
    log = (
        "13:00:00.000 (1000)|EXECUTION_STARTED\n"
        "garbage line with no timestamp\n"
        "13:00:00.001 (1001)|EXECUTION_FINISHED\n"
    )
    _, stats = parse(log)
    assert stats.unclassified == 1


def test_event_kind_enum_round_trip() -> None:
    assert LogEventKind("DML_BEGIN") is LogEventKind.DML_BEGIN
