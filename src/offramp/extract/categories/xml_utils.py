"""Tiny shared XML helpers for the per-category extractors.

The Salesforce metadata XML namespace is constant across files; the helpers
strip it so downstream code can use plain tag names.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

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
            # Promote to list on second occurrence.
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
