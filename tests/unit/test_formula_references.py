from __future__ import annotations

from offramp.generate.formula.parser import parse
from offramp.generate.formula.references import extract_references


def test_fields_globals_functions() -> None:
    r = extract_references(
        'AND(ISPICKVAL(LeadSource, "Web"), ISBLANK(Email), NOT($Permission.Bypass_Validation))'
    )
    assert r.parsed
    assert r.fields == ["Email", "LeadSource"]
    assert r.globals == ["$Permission.Bypass_Validation"]
    assert r.functions == ["AND", "ISBLANK", "ISPICKVAL", "NOT"]
    assert r.qualified_fields("Lead") == ["Lead.Email", "Lead.LeadSource"]


def test_relationship_paths_and_new_functions() -> None:
    r = extract_references(
        "PRIORVALUE(Owner.Manager.Email) <> Owner.Email && ISCHANGED(StageName) && $User.ProfileId <> $Setup.X__c.Y__c"
    )
    assert r.parsed
    assert r.fields == ["Owner.Email", "Owner.Manager.Email", "StageName"]
    assert r.globals == ["$Setup.X__c.Y__c", "$User.ProfileId"]
    assert {"ISCHANGED", "PRIORVALUE"} <= set(r.functions)


def test_concat_operator_parses() -> None:
    assert parse('FirstName & " " & LastName') is not None


def test_tolerant_fallback_on_unsupported_syntax() -> None:
    r = extract_references("CASE(Type, 'a', Amount, [unsupported] + Discount__c)")
    assert not r.parsed
    assert "Amount" in r.fields and "Discount__c" in r.fields and "Type" in r.fields
    assert "CASE" in r.functions
