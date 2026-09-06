"""Knowledge store (C24): the persistent, reusable process library.

Every scan adds its processes to a content-addressed library; identical
logic across scans and orgs is one entry with many sources. See
:mod:`offramp.knowledge.store` (file-backed, no services) and
:mod:`offramp.knowledge.falkor` (persistent graph for exploration).
"""

from offramp.knowledge.store import KnowledgeStore, ScanRecord

__all__ = ["KnowledgeStore", "ScanRecord"]
