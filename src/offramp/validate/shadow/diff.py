"""Field-level diff between two record snapshots."""

from __future__ import annotations

from typing import Any


def field_diff(
    production: dict[str, Any],
    runtime: dict[str, Any],
    *,
    ignore: set[str] | None = None,
) -> dict[str, tuple[Any, Any]]:
    """Return ``{field: (prod_value, runtime_value)}`` for differing fields.

    Salesforce silently treats missing fields as nulls; absent vs. None vs. ''
    are normalized to None for comparison so we don't flag a field that
    appears in one snapshot but not the other unless the value actually
    changed.
    """
    ignore_set = ignore or set()
    keys = set(production) | set(runtime)
    out: dict[str, tuple[Any, Any]] = {}
    for k in keys:
        if k in ignore_set:
            continue
        p = _normalize(production.get(k))
        r = _normalize(runtime.get(k))
        if p != r:
            out[k] = (p, r)
    return out


def _normalize(v: Any) -> Any:
    if v is None or v == "":
        return None
    return v
