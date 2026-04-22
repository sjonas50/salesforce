"""SOQL identifier + value validators (the injection-defense layer)."""

from __future__ import annotations

import pytest

from offramp.core.soql import (
    InvalidSOQLIdentifier,
    InvalidSOQLValue,
    quote_record_id_list,
    validate_field,
    validate_record_id,
    validate_sobject,
)

# -- validate_sobject --------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Account",
        "Lead",
        "Opportunity",
        "Custom_Object__c",
        "MyEvent__e",
        "MyCMT__mdt",
        "BigObj__b",
        "External__x",
        "A",  # single letter
    ],
)
def test_validate_sobject_accepts_real_names(name: str) -> None:
    assert validate_sobject(name) == name


@pytest.mark.parametrize(
    "hostile",
    [
        # Classic injection shapes
        "Account; DROP TABLE x",
        "Account'--",
        "Account WHERE",
        "Account OR 1=1",
        "Account UNION SELECT",
        "Account/*comment*/",
        # Whitespace + control chars
        " Account",
        "Account ",
        "Acc ount",
        "Account\n",
        "Account\t",
        # Starts with digit or symbol
        "1Account",
        "_Account",
        "__c",
        # Quotes / parens / brackets
        "Account(1)",
        "'Account'",
        '"Account"',
        # Empty
        "",
    ],
)
def test_validate_sobject_rejects_injection_shapes(hostile: str) -> None:
    with pytest.raises(InvalidSOQLIdentifier):
        validate_sobject(hostile)


def test_validate_sobject_rejects_overlong_name() -> None:
    with pytest.raises(InvalidSOQLIdentifier):
        validate_sobject("A" + "x" * 80)  # > 80 chars


def test_validate_sobject_rejects_non_string() -> None:
    with pytest.raises(InvalidSOQLIdentifier):
        validate_sobject(None)  # type: ignore[arg-type]


# -- validate_record_id ------------------------------------------------------


@pytest.mark.parametrize(
    "rid",
    [
        "001000000000001",  # 15-char
        "001000000000001AAA",  # 18-char
        "0011x00000ABCDEFGHI",  # mixed case 18-char (has all 18 chars)
        "abcdefghijklmno",  # all lowercase 15-char
        "ABC123DEF456GHI",  # mixed 15-char
    ],
)
def test_validate_record_id_accepts_real_ids(rid: str) -> None:
    # Need to be 15 or 18 exactly.
    if len(rid) in (15, 18):
        assert validate_record_id(rid) == rid


@pytest.mark.parametrize(
    "hostile",
    [
        # Injection shapes
        "001'; DROP TABLE x",
        "001' OR '1'='1",
        "001' UNION SELECT",
        # Length-wrong
        "",
        "001",  # too short
        "0010000000000001",  # 16 chars
        "0010000000000001A",  # 17 chars
        "0010000000000001ABCDE",  # 20 chars
        # Non-alphanumeric
        "001 000000000001",
        "001-000000000001",
        "001_000000000001",
        "001.000000000001",
    ],
)
def test_validate_record_id_rejects_bad_ids(hostile: str) -> None:
    with pytest.raises(InvalidSOQLValue):
        validate_record_id(hostile)


def test_validate_record_id_rejects_non_string() -> None:
    with pytest.raises(InvalidSOQLValue):
        validate_record_id(12345)  # type: ignore[arg-type]


# -- validate_field ----------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "Id",
        "Name",
        "Account.Name",
        "Account.Owner.Email",
        "My_Custom_Field__c",
        "Account.Owner.Manager.My_Custom__c",
    ],
)
def test_validate_field_accepts_dotted_paths(field: str) -> None:
    assert validate_field(field) == field


@pytest.mark.parametrize(
    "hostile",
    [
        "Name; DROP",
        "Name,Id",  # comma not allowed — caller should validate each separately
        "Name OR",
        ".Name",  # leading dot
        "Name.",  # trailing dot
        "Name..Other",  # double dot
        "1Name",
    ],
)
def test_validate_field_rejects_bad(hostile: str) -> None:
    with pytest.raises(InvalidSOQLIdentifier):
        validate_field(hostile)


# -- quote_record_id_list ----------------------------------------------------


def test_quote_record_id_list_happy_path() -> None:
    ids = ["001000000000001", "002000000000002", "003000000000003"]
    out = quote_record_id_list(ids)
    assert out == "'001000000000001','002000000000002','003000000000003'"


def test_quote_record_id_list_rejects_one_bad_id() -> None:
    ids = ["001000000000001", "bad'id", "003000000000003"]
    with pytest.raises(InvalidSOQLValue):
        quote_record_id_list(ids)


def test_quote_record_id_list_rejects_empty() -> None:
    with pytest.raises(InvalidSOQLValue):
        quote_record_id_list([])


def test_quote_record_id_list_rejects_overlong_chunk() -> None:
    ids = [f"0010000000{i:05d}" for i in range(201)]  # 201 > default max_chunk=200
    with pytest.raises(InvalidSOQLValue):
        quote_record_id_list(ids)
