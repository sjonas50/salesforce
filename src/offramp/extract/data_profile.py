"""Data-level evidence (build plan D.8): record counts and field fill rates.

Metadata says where a field *could* be used; the data says whether anyone
ever put a value in it. One ``limits/recordCount`` call gives approximate
record counts for many objects at once; one aggregate query per object
(chunked by field) gives non-null counts per custom field. Both are cheap
against the API quota compared with the value they add to the unused-field
verdict.

Aggregate ``COUNT(field)`` is not allowed on long text, rich text, encrypted,
or multi-select picklist fields; those are skipped and reported as unknown.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import DataProfile, FieldProfile, ObjectProfile, SchemaSnapshot

log = get_logger(__name__)

# Compared case-insensitively: describe says ``textarea``/``boolean``, the
# metadata snapshot says ``TextArea``/``Checkbox``.
_UNAGGREGATABLE_TYPES = {
    t.lower()
    for t in (
        "LongTextArea",
        "TextArea",
        "Html",
        "RichTextArea",
        "EncryptedText",
        "MultiselectPicklist",
        "Location",
        "Address",
        "Checkbox",
        "boolean",
        "textarea",
        "base64",
        "encryptedstring",
        "multipicklist",
    )
}
_UNSUPPORTED_RE = re.compile(r"field (\w+) does not support aggregate operator")
_SKIP_OBJECT_SUFFIXES = ("__mdt", "__e", "__b", "__x", "Share", "History", "Feed", "ChangeEvent")


def profile_from_dump(rows: Any, *, org_alias: str) -> DataProfile:
    """Load a ``_tooling/data_profile.json`` dump (fixtures, SFDX projects)."""
    prof = DataProfile(org_alias=org_alias, source="dump")
    if not isinstance(rows, dict):
        return prof
    for obj, spec in rows.items():
        if not isinstance(spec, dict):
            continue
        fields: dict[str, FieldProfile] = {}
        count = spec.get("record_count")
        for fname, non_null in (spec.get("fields") or {}).items():
            q = f"{obj}.{fname}"
            nn = int(non_null)
            rate = (nn / count) if count else 0.0
            fields[q] = FieldProfile(api_name=q, non_null=nn, fill_rate=max(0.0, min(1.0, rate)))
        lm = spec.get("last_modified")
        prof.objects[obj] = ObjectProfile(
            object_name=obj,
            record_count=int(count) if count is not None else None,
            last_modified=datetime.fromisoformat(lm) if isinstance(lm, str) else None,
            fields=fields,
        )
    return prof


async def profile_from_gateway(
    gateway: Any,
    schema: SchemaSnapshot,
    *,
    org_alias: str,
    fields_per_query: int = 40,
    max_objects: int = 400,
) -> DataProfile:
    """Profile every custom-field-bearing object in ``schema`` through the MCP gateway."""
    prof = DataProfile(org_alias=org_alias)
    by_object: dict[str, list[Any]] = {}
    for f in schema.fields():
        if f.custom and f.object_name and not f.object_name.endswith(_SKIP_OBJECT_SUFFIXES):
            by_object.setdefault(f.object_name, []).append(f)
    names = sorted(by_object)[:max_objects]

    counts = await _record_counts(gateway, names)
    for obj in names:
        op = ObjectProfile(object_name=obj, record_count=counts.get(obj))
        prof.objects[obj] = op
        aggregatable = [
            f for f in by_object[obj] if (f.field_type or "").lower() not in _UNAGGREGATABLE_TYPES
        ]
        for i in range(0, len(aggregatable), fields_per_query):
            chunk = aggregatable[i : i + fields_per_query]
            resp = None
            for _attempt in range(len(chunk) + 1):
                selects = ", ".join(
                    f"COUNT({f.api_name.split('.', 1)[1]}) c{k}" for k, f in enumerate(chunk)
                )
                soql = f"SELECT COUNT(Id) total, MAX(LastModifiedDate) lm, {selects} FROM {obj}"
                try:
                    resp = await gateway.sf_query(soql)
                    break
                except Exception as exc:  # one failing object must not sink the profile
                    # Salesforce names the field it refuses to COUNT; drop it and retry so
                    # a type this list does not know about costs one field, not the object.
                    m = _UNSUPPORTED_RE.search(str(exc))
                    bad = m.group(1) if m else None
                    if bad and any(f.api_name.split(".", 1)[1] == bad for f in chunk):
                        log.info(
                            "extract.data_profile.field_unaggregatable", sobject=obj, field=bad
                        )
                        chunk = [f for f in chunk if f.api_name.split(".", 1)[1] != bad]
                        if chunk:
                            continue
                    op.error = str(exc)[:200]
                    log.warning(
                        "extract.data_profile.aggregate_failed", sobject=obj, error=str(exc)
                    )
                    break
            if resp is None:
                break
            rows = resp.get("records") or []
            if not rows:
                continue
            row = rows[0]
            total = int(row.get("total") or 0)
            if op.record_count is None:
                op.record_count = total
            lm = row.get("lm")
            if isinstance(lm, str) and op.last_modified is None:
                with contextlib.suppress(ValueError):
                    op.last_modified = datetime.fromisoformat(lm.replace("Z", "+00:00"))
            for k, f in enumerate(chunk):
                nn = int(row.get(f"c{k}") or 0)
                rate = (nn / total) if total else 0.0
                op.fields[f.api_name] = FieldProfile(
                    api_name=f.api_name, non_null=nn, fill_rate=max(0.0, min(1.0, rate))
                )
    log.info(
        "extract.data_profile.done",
        objects=len(prof.objects),
        fields=sum(len(o.fields) for o in prof.objects.values()),
    )
    return prof


async def _record_counts(gateway: Any, names: list[str]) -> dict[str, int]:
    """Approximate counts from ``limits/recordCount`` (one call per 100 objects)."""
    out: dict[str, int] = {}
    for i in range(0, len(names), 100):
        chunk = names[i : i + 100]
        try:
            resp = await gateway.sf_restful("limits/recordCount", {"sObjects": ",".join(chunk)})
        except Exception as exc:
            log.warning("extract.data_profile.record_count_failed", error=str(exc))
            continue
        for row in (resp or {}).get("sObjects", []) if isinstance(resp, dict) else []:
            if isinstance(row, dict) and row.get("name"):
                out[str(row["name"])] = int(row.get("count") or 0)
    return out
