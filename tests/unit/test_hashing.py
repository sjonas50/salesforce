"""Phase 0.7: canonical hashing must be deterministic across input order."""

from __future__ import annotations

from offramp.core.hashing import canonical_json, content_hash


def test_canonical_json_sorts_keys() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_content_hash_stable_across_equivalent_inputs() -> None:
    a = {"name": "X", "category": "validation_rule", "items": [3, 2, 1]}
    b = {"items": [3, 2, 1], "category": "validation_rule", "name": "X"}
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_with_value_change() -> None:
    a = {"name": "X"}
    b = {"name": "Y"}
    assert content_hash(a) != content_hash(b)


def test_canonical_json_no_whitespace() -> None:
    assert b" " not in canonical_json({"a": [1, 2, 3]})
