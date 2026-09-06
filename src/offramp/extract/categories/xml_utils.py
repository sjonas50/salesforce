"""Tiny shared XML / payload helpers for the per-category extractors.

The Salesforce metadata XML namespace is constant across files; the helpers
strip it so downstream code can use plain tag names. The Tooling API returns
the same structures as JSON (``Metadata`` field); :func:`get_body` accepts
either so every extractor is source-agnostic (AD-29).
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from offramp.extract.pull.reconciler import ReconciledRecord

_NS_RE = re.compile(r"^\{[^}]+\}")


def strip_ns(tag: str) -> str:
    """Drop the XML namespace prefix from a tag name."""
    return _NS_RE.sub("", tag)


def element_to_dict(elem: ET.Element) -> dict[str, Any] | str:
    """Recursive ElementTree → dict conversion suitable for canonical hashing.

    Repeated child tags become lists; leaves are strings (text content).
    The namespace prefix is stripped so we don't leak it into hashes.
    """
    children = list(elem)
    if not children:
        return (elem.text or "").strip()

    out: dict[str, Any] = {}
    for child in children:
        key = strip_ns(child.tag)
        value = element_to_dict(child)
        if key in out:
            existing = out[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[key] = [existing, value]
        else:
            out[key] = value
    return out


def parse_xml(raw: str) -> dict[str, Any]:
    """Parse an XML document and return a dict keyed by the (de-namespaced) root tag."""
    root = ET.fromstring(raw)
    body = element_to_dict(root)
    if isinstance(body, str):
        return {strip_ns(root.tag): body}
    return {strip_ns(root.tag): body}


def get_body(record: ReconciledRecord, root_tag: str) -> dict[str, Any]:
    """Return the metadata body for ``record`` regardless of source shape.

    * ``payload['parsed']`` (dict) — already-structured metadata (Tooling API JSON)
    * ``payload['raw_xml']`` (str) — Metadata API / source-format XML

    Raises :class:`ValueError` when neither is present or the root is malformed.
    """
    parsed = record.payload.get("parsed")
    if isinstance(parsed, dict):
        body = parsed.get(root_tag, parsed)
        if isinstance(body, dict):
            return body
        raise ValueError(f"{root_tag} {record.api_name}: parsed payload malformed")
    raw = record.payload.get("raw_xml")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{root_tag} {record.api_name} missing raw_xml")
    try:
        doc = parse_xml(raw)
    except ET.ParseError as exc:
        raise ValueError(f"{root_tag} {record.api_name}: XML parse error: {exc}") from exc
    body = doc.get(root_tag)
    if not isinstance(body, dict):
        # Empty root element (e.g. a bare <ApexClass/>) parses to "".
        if body == "" or (body is None and len(doc) == 1):
            return {}
        raise ValueError(f"{root_tag} {record.api_name}: XML root malformed (got {list(doc)})")
    return body


def as_list(v: Any) -> list[Any]:
    """SF XML repeats elements rather than wrapping in arrays — normalize."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return default
    return str(v).strip().lower() == "true"


def as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, dict | list):
        return default
    return str(v)


def object_from_path(path: str) -> str:
    """``objects/<Object>/...`` → ``<Object>``; ``<Object>.workflow-meta.xml`` → ``<Object>``."""
    parts = path.split("/")
    if "objects" in parts:
        i = parts.index("objects")
        if i + 1 < len(parts):
            return parts[i + 1]
    leaf = parts[-1] if parts else ""
    for suffix in (
        ".workflow-meta.xml",
        ".assignmentRules-meta.xml",
        ".autoResponseRules-meta.xml",
        ".escalationRules-meta.xml",
        ".sharingRules-meta.xml",
    ):
        if leaf.endswith(suffix):
            return leaf[: -len(suffix)]
    return ""


def criteria_items(items: Any) -> list[dict[str, str]]:
    """Normalize ``<criteriaItems>`` blocks shared by workflow, assignment, escalation, sharing."""
    return [
        {
            "field": as_str(c.get("field")),
            "operation": as_str(c.get("operation")),
            "value": as_str(c.get("value")),
            "value_field": as_str(c.get("valueField")),
        }
        for c in as_list(items)
        if isinstance(c, dict)
    ]


def criteria_fields(items: list[dict[str, str]]) -> list[str]:
    """Field API names referenced by criteria items ('Account.AnnualRevenue' → as-is)."""
    out: list[str] = []
    for c in items:
        for key in ("field", "value_field"):
            f = c.get(key)
            if f:
                out.append(f)
    return sorted(set(out))
