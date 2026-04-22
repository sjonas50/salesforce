"""SOQL identifier + value validators.

The Salesforce REST API doesn't support parameter binding — SOQL goes over
the wire as a string. That means **caller-side validation is the only
defense** against injection when any user- or wire-controlled value
reaches a SOQL builder.

Primary attack surface:

* ``sobject`` derived from CDC event headers (``entity_name``) — a tampered
  Pub/Sub stream could inject `` '; DROP ...`` or cross-object leaks.
* ``record_id`` from ``ChangeEventHeader.record_ids`` — same threat model.

The validators below return the value unchanged on success and raise
:class:`InvalidSOQLIdentifier` / :class:`InvalidSOQLValue` on failure.
Call them BEFORE interpolating into an f-string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# SOQL-identifier rule: must start with a letter, then letters / digits /
# underscores only. Salesforce custom objects end in ``__c``; platform
# events end in ``__e``; custom metadata types end in ``__mdt``. The
# pattern covers all three plus standard objects.
#
# IMPORTANT: use ``\Z`` not ``$`` — Python's ``$`` also matches before a
# trailing newline, which would let ``"Account\n"`` through.
_SOBJECT_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*(?:__c|__e|__mdt|__b|__x)?\Z")

# Salesforce record IDs are EXACTLY 15 or 18 chars (not 15-to-18) —
# there is no such thing as a 16- or 17-char record id.
_RECORD_ID_RE = re.compile(r"\A(?:[A-Za-z0-9]{15}|[A-Za-z0-9]{18})\Z")

# A SOQL field name: same identifier rule but allows dotted references
# (Account.Owner.Email) for cross-object selects.
_FIELD_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*\Z")


class InvalidSOQLIdentifier(ValueError):
    """Raised when an sObject / field name doesn't match the strict pattern."""


class InvalidSOQLValue(ValueError):
    """Raised when a value (e.g. record id) fails validation."""


def validate_sobject(name: str) -> str:
    """Return ``name`` unchanged if it matches a safe sObject identifier.

    Enforces length (≤ 80 chars — SF's limit) plus the character class.
    """
    if not isinstance(name, str):
        raise InvalidSOQLIdentifier(f"sObject must be a string; got {type(name).__name__}")
    if len(name) > 80:
        raise InvalidSOQLIdentifier(f"sObject name longer than 80 chars: {name!r}")
    if not _SOBJECT_RE.match(name):
        raise InvalidSOQLIdentifier(f"invalid sObject identifier: {name!r}")
    return name


def validate_record_id(rid: str) -> str:
    """Return ``rid`` unchanged if it matches the SF record-id pattern.

    Accepts both 15- and 18-char forms.
    """
    if not isinstance(rid, str):
        raise InvalidSOQLValue(f"record id must be a string; got {type(rid).__name__}")
    if not _RECORD_ID_RE.match(rid):
        raise InvalidSOQLValue(f"invalid Salesforce record id: {rid!r}")
    return rid


def validate_field(field: str) -> str:
    """Validate a SOQL field name, incl. cross-object dotted references."""
    if not isinstance(field, str):
        raise InvalidSOQLIdentifier(f"field must be a string; got {type(field).__name__}")
    if len(field) > 255:
        raise InvalidSOQLIdentifier(f"field path longer than 255 chars: {field[:40]!r}...")
    if not _FIELD_RE.match(field):
        raise InvalidSOQLIdentifier(f"invalid field identifier: {field!r}")
    return field


def quote_record_id_list(ids: Iterable[str], *, max_chunk: int = 200) -> str:
    """Build a ``IN (...)`` clause body from a validated list of record ids.

    Returns a comma-joined list of single-quoted ids. Raises
    :class:`InvalidSOQLValue` on the first bad id. Enforces
    ``max_chunk`` (the SF SOQL IN-list limit is 200).
    """
    validated = [validate_record_id(r) for r in ids]
    if len(validated) == 0:
        raise InvalidSOQLValue("record id list is empty")
    if len(validated) > max_chunk:
        raise InvalidSOQLValue(
            f"record id list has {len(validated)} entries; SOQL limit is {max_chunk}"
        )
    # Safe to single-quote: record ids are already alphanumeric-only.
    return ",".join(f"'{r}'" for r in validated)
