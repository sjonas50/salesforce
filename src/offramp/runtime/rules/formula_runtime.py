"""Runtime helpers used by emitted Python rule code.

The formula emitter generates code that calls these helpers — keeping them
in one module means generated artifacts stay tiny and the helpers are
unit-tested in isolation.

Salesforce semantics being preserved:
* dotted field refs walk the record dict (None for any missing segment)
* ``ISBLANK`` is true for None, '', whitespace-only strings (per SF docs)
* ``ISPICKVAL`` is exact-match on the picklist value
* numeric helpers default missing values to 0 (per ``BlankAsZero`` policy)
* ``BlankAsBlank`` policy is opt-in via ``context['blank_policy']``
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any


def _field(record: dict[str, Any], path: str) -> Any:
    """Walk ``record`` along the dotted ``path`` ('Account.Owner.Email').

    Returns ``None`` if any segment is missing.
    """
    cur: Any = record
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _isblank(v: Any) -> bool:
    """SF-compatible ISBLANK: None, '', or all-whitespace string is blank."""
    if v is None:
        return True
    if isinstance(v, str):
        stripped: str = v.strip()
        return stripped == ""
    return False


def _ispickval(picklist_value: Any, target: Any) -> bool:
    return bool(picklist_value == target)


def _blankvalue(v: Any, fallback: Any) -> Any:
    """SF BLANKVALUE / NULLVALUE: return fallback when v is blank."""
    return fallback if _isblank(v) else v


def _round(v: float, digits: int = 0) -> float:
    return round(float(v or 0), int(digits))


def _floor(v: float) -> int:
    return math.floor(float(v or 0))


def _ceil(v: float) -> int:
    return math.ceil(float(v or 0))


def _mod(a: float, b: float) -> float:
    return float(a or 0) % float(b or 1)


def _upper(s: Any) -> str:
    return str(s or "").upper()


def _lower(s: Any) -> str:
    return str(s or "").lower()


def _trim(s: Any) -> str:
    return str(s or "").strip()


def _left(s: Any, n: int) -> str:
    return str(s or "")[: int(n)]


def _right(s: Any, n: int) -> str:
    return str(s or "")[-int(n) :] if int(n) > 0 else ""


def _mid(s: Any, start: int, length: int) -> str:
    text = str(s or "")
    # SF's MID() is 1-indexed.
    s0 = max(int(start) - 1, 0)
    return text[s0 : s0 + int(length)]


def _substitute(s: Any, old: str, new: str) -> str:
    return str(s or "").replace(str(old), str(new))


def _find(needle: str, haystack: Any) -> int:
    """1-indexed; returns 0 when not found, matching SF semantics."""
    text = str(haystack or "")
    idx = text.find(str(needle))
    return idx + 1 if idx >= 0 else 0


def _begins(s: Any, prefix: Any) -> bool:
    return str(s or "").startswith(str(prefix or ""))


def _contains(haystack: Any, needle: Any) -> bool:
    return str(needle or "") in str(haystack or "")


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _value(v: Any) -> float:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return 0.0
    return float(v)


def _today() -> _dt.date:
    return _dt.date.today()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _date(year: int, month: int, day: int) -> _dt.date:
    return _dt.date(int(year), int(month), int(day))


def _addmonths(d: _dt.date | _dt.datetime | None, months: int) -> _dt.date | None:
    """SF ADDMONTHS — clamps day-of-month when target month is shorter."""
    if d is None:
        return None
    base_year = d.year
    target = d.month + int(months)
    year_delta, month0 = divmod(target - 1, 12)
    month = month0 + 1
    year = base_year + year_delta
    # Clamp to last valid day of the target month.
    import calendar

    last = calendar.monthrange(year, month)[1]
    day = min(d.day, last)
    if isinstance(d, _dt.datetime):
        return d.replace(year=year, month=month, day=day)
    return _dt.date(year, month, day)
