"""Per-category extractors driven from raw metadata records.

Importing this package eagerly registers every extractor in the
:mod:`offramp.extract.categories.base` registry.
"""

from offramp.extract.categories import (
    _passthrough,  # noqa: F401 — side-effect registration
    apex_trigger,  # noqa: F401
    flow,  # noqa: F401
    formula_field,  # noqa: F401
    validation_rule,  # noqa: F401
)
