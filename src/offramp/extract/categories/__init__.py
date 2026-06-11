"""Per-category extractors driven from raw metadata records.

Importing this package eagerly registers every extractor in the
:mod:`offramp.extract.categories.base` registry.
"""

# Each module self-registers on import. _passthrough's fallback extractors and
# the specific extractors below cover disjoint categories, so import order does
# not affect the final registry. The LWC extractor lives in extract.lwc and is
# imported here (not via _passthrough) to avoid a bundle ↔ _passthrough cycle.
from offramp.extract.categories import (
    _passthrough,  # noqa: F401 — side-effect registration
    apex_trigger,  # noqa: F401
    assignment_rule,  # noqa: F401
    flow,  # noqa: F401
    formula_field,  # noqa: F401
    validation_rule,  # noqa: F401
    workflow_rule,  # noqa: F401
)
from offramp.extract.lwc import bundle  # noqa: F401 — registers LWCBundleExtractor
