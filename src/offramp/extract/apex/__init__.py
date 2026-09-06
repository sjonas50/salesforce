"""Apex static analysis (C20, AD-31).

Tokenizer-based reference extraction. The public contract is
:class:`offramp.extract.apex.model.ApexAnalysis`; a grammar-backed parser can
replace :mod:`offramp.extract.apex.references` later without touching consumers.
"""

from offramp.extract.apex.model import ApexAnalysis, DmlRef, SoqlRef
from offramp.extract.apex.references import analyze

__all__ = ["ApexAnalysis", "DmlRef", "SoqlRef", "analyze"]
