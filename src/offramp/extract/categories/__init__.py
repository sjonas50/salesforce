"""Per-category extractors driven from raw metadata records.

Importing this package eagerly registers every extractor in the
:mod:`offramp.extract.categories.base` registry.
"""

# _passthrough must be imported LAST so specific extractors (above) register
# before the passthrough fallbacks for any categories they don't cover.
from offramp.extract.categories import (
    _passthrough,  # noqa: F401
    apex_trigger,  # noqa: F401 — side-effect registration
    assignment_rule,  # noqa: F401
    flow,  # noqa: F401
    formula_field,  # noqa: F401
    validation_rule,  # noqa: F401
    workflow_rule,  # noqa: F401
)
