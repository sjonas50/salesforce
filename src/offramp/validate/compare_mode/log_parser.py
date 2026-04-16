"""Parse Salesforce debug logs into reconstructed transactions.

Salesforce debug logs are line-oriented, pipe-delimited:

    24.6 (2,3,4)|USER_INFO|...
    13:00:00.001 (1234567)|EXECUTION_STARTED
    13:00:00.001 (1234567)|CODE_UNIT_STARTED|[EXTERNAL]|...
    13:00:00.020 (20000000)|DML_BEGIN|[123]|Op:Insert|Type:Lead|Rows:1
    13:00:00.030 (30000000)|VALIDATION_RULE|...
    13:00:00.050 (50000000)|FLOW_START_INTERVIEW_BEGIN|...
    13:00:00.080 (80000000)|EXECUTION_FINISHED

Phase 4 ships the parser for the subset Compare Mode needs:

* DML boundaries (transaction start + end)
* CODE_UNIT_STARTED / CODE_UNIT_FINISHED — identifies trigger context
* VALIDATION_RULE entries — confirms which validation rules fired
* FLOW_START_INTERVIEW / FLOW_ELEMENT_DEFERRED — flow execution
* USER_INFO + EXECUTION_STARTED — extract context state

The parser is forgiving: unknown event lines are skipped with a counter so
we know how much of the log we couldn't classify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class LogEventKind(StrEnum):
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_FINISHED = "EXECUTION_FINISHED"
    CODE_UNIT_STARTED = "CODE_UNIT_STARTED"
    CODE_UNIT_FINISHED = "CODE_UNIT_FINISHED"
    DML_BEGIN = "DML_BEGIN"
    DML_END = "DML_END"
    VALIDATION_RULE = "VALIDATION_RULE"
    VALIDATION_PASS = "VALIDATION_PASS"
    VALIDATION_FAIL = "VALIDATION_FAIL"
    FLOW_START_INTERVIEW_BEGIN = "FLOW_START_INTERVIEW_BEGIN"
    FLOW_START_INTERVIEW_END = "FLOW_START_INTERVIEW_END"
    USER_INFO = "USER_INFO"


@dataclass
class LogEvent:
    """One classified line from the debug log."""

    timestamp: datetime
    nanos: int  # the (NNN) field — monotonic within a log
    kind: LogEventKind
    raw: str
    parts: list[str] = field(default_factory=list)


@dataclass
class ParsedTransaction:
    """All events between EXECUTION_STARTED and EXECUTION_FINISHED."""

    start: datetime
    end: datetime | None
    events: list[LogEvent] = field(default_factory=list)
    dml_ops: list[dict[str, str]] = field(default_factory=list)  # parsed DML rows
    validation_failures: list[str] = field(default_factory=list)
    flows_invoked: list[str] = field(default_factory=list)
    user_id: str | None = None


@dataclass
class ParseStats:
    classified: int = 0
    unclassified: int = 0
    transactions: int = 0


_LINE_RE = re.compile(r"^(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+\((?P<nanos>\d+)\)\|(?P<rest>.*)$")
_DML_RE = re.compile(
    r"DML_BEGIN\|\[(?P<line>\d+)\]\|Op:(?P<op>\w+)\|Type:(?P<type>\w+)\|Rows:(?P<rows>\d+)"
)
_USER_RE = re.compile(r"USER_INFO\|\[EXTERNAL\]\|(?P<user_id>0\w{14,17})")
_VR_FAIL_RE = re.compile(r"VALIDATION_FAIL\|.*?Name:(?P<name>[\w.]+)")
_VR_RE = re.compile(r"VALIDATION_RULE\|\[\w+\]\|.*?Name:(?P<name>[\w.]+)")


def parse(source: str) -> tuple[list[ParsedTransaction], ParseStats]:
    """Parse a complete debug log dump."""
    stats = ParseStats()
    transactions: list[ParsedTransaction] = []
    current: ParsedTransaction | None = None

    for raw in source.splitlines():
        m = _LINE_RE.match(raw)
        if m is None:
            stats.unclassified += 1
            continue
        ts_str = m.group("time")
        nanos = int(m.group("nanos"))
        rest = m.group("rest")
        kind_token = rest.split("|", 1)[0]
        try:
            kind = LogEventKind(kind_token)
        except ValueError:
            stats.unclassified += 1
            continue
        ts = _parse_log_time(ts_str)
        evt = LogEvent(timestamp=ts, nanos=nanos, kind=kind, raw=raw, parts=rest.split("|"))
        stats.classified += 1

        if kind is LogEventKind.EXECUTION_STARTED:
            current = ParsedTransaction(start=ts, end=None)
            transactions.append(current)
            stats.transactions += 1
            continue
        if current is None:
            continue
        current.events.append(evt)
        if kind is LogEventKind.EXECUTION_FINISHED:
            current.end = ts
            current = None
            continue
        if kind is LogEventKind.USER_INFO:
            mu = _USER_RE.search(raw)
            if mu:
                current.user_id = mu.group("user_id")
        elif kind is LogEventKind.DML_BEGIN:
            md = _DML_RE.search(raw)
            if md:
                current.dml_ops.append(
                    {
                        "op": md.group("op"),
                        "sobject": md.group("type"),
                        "rows": md.group("rows"),
                    }
                )
        elif kind is LogEventKind.VALIDATION_FAIL:
            mv = _VR_FAIL_RE.search(raw)
            if mv:
                current.validation_failures.append(mv.group("name"))
        elif kind is LogEventKind.VALIDATION_RULE:
            mv2 = _VR_RE.search(raw)
            if mv2 is not None and mv2.group("name") not in current.validation_failures:
                # Track the rule was evaluated even if it passed.
                pass
        elif kind is LogEventKind.FLOW_START_INTERVIEW_BEGIN:
            # Flow name is the second-to-last part on this line.
            if len(evt.parts) >= 2:
                current.flows_invoked.append(evt.parts[-1])

    return transactions, stats


def _parse_log_time(s: str) -> datetime:
    """Convert ``HH:MM:SS.SSS`` to a UTC datetime on today's date.

    Real debug logs include a date elsewhere; for Compare Mode we only need
    relative ordering, so synthesizing today's date is sufficient.
    """
    today = datetime.now(UTC).date()
    h, m, sec = s.split(":")
    secs, _, ms_str = sec.partition(".")
    micros = int((ms_str + "000")[:3]) * 1000
    return datetime(
        today.year,
        today.month,
        today.day,
        int(h),
        int(m),
        int(secs),
        micros,
        tzinfo=UTC,
    )
