"""Validation Rule extractor — canonical-shape regression."""

from __future__ import annotations

from offramp.core.models import CategoryName
from offramp.extract.categories.base import get_extractor
from offramp.extract.categories.validation_rule import ValidationRuleExtractor  # noqa: F401
from offramp.extract.pull.reconciler import ReconciledRecord


def test_validation_rule_parses_canonical_shape() -> None:
    record = ReconciledRecord(
        category=CategoryName.VALIDATION_RULE,
        api_name="Industry_Required",
        namespace=None,
        payload={
            "path": "objects/Account/validationRules/Industry_Required.validationRule-meta.xml",
            "raw_xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata">'
                "  <fullName>Industry_Required</fullName>"
                "  <active>true</active>"
                "  <errorConditionFormula>ISBLANK(Industry)</errorConditionFormula>"
                "  <errorDisplayField>Industry</errorDisplayField>"
                "  <errorMessage>Industry is required.</errorMessage>"
                "</ValidationRule>"
            ),
        },
    )
    extractor = get_extractor(CategoryName.VALIDATION_RULE)
    parsed = extractor.parse_payload(record)

    assert parsed["object"] == "Account"
    assert parsed["active"] is True
    assert parsed["error_condition_formula"] == "ISBLANK(Industry)"
    assert parsed["error_display_field"] == "Industry"
    assert parsed["error_message"] == "Industry is required."
