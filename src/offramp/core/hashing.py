"""Canonical content hashing.

The Engram anchor key for any artifact is ``sha256(canonical_json(payload))``.
"Canonical" here means: keys sorted, ASCII-only escapes off, no whitespace.
Two byte-identical canonical JSON renderings produce the same hash regardless
of language or library.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> bytes:
    """Render ``payload`` to canonical JSON bytes.

    Used for both content hashing and Engram payload encoding.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_default,
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    """SHA-256 hex digest of ``payload`` rendered as canonical JSON."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _default(obj: Any) -> Any:
    """JSON serializer fallback for non-primitive types."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
