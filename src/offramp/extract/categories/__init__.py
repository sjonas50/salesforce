"""Per-category extractors driven from raw metadata records.

Registration is lazy: :func:`offramp.extract.categories.base.get_extractor`
imports every extractor module on first use. Nothing here imports the
extractors eagerly, so importing any single extractor module never re-enters
this package mid-initialization.
"""
