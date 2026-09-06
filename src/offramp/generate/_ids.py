"""Shared identifier sanitization for generated code."""

from __future__ import annotations

import re


def safe_id(s: str, *, prefix: str = "r_") -> str:
    """Sanitize an arbitrary Salesforce developer name into a Python identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{prefix}{cleaned}"
    return cleaned
