"""Custom Metadata Type record reader.

Trigger Actions Framework / metadata-driven dispatch patterns store the real
handler graph in CMT records (e.g. ``Trigger_Action__mdt`` rows). Static
analysis of the trigger code returns a single dispatcher class; this reader
recovers the full handler list by querying the CMT records themselves.

Phase 1 ships against fixtures (CMT rows pre-loaded into a JSON file under
``_tooling/cmt_records.json``). Real Tooling API integration lands once the
:class:`offramp.extract.pull.tooling_api.ToolingApiPullClient` is wired up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CMTRecord:
    """One row from a custom metadata type."""

    cmt_type: str  # e.g. 'Trigger_Action__mdt'
    developer_name: str
    fields: dict[str, str]


def read_cmt_records_from_fixture(root: Path) -> list[CMTRecord]:
    """Load CMT rows from ``<root>/_tooling/cmt_records.json``.

    Schema::

        [
          {
            "cmt_type": "Trigger_Action__mdt",
            "developer_name": "Lead_Insert_001",
            "fields": {
              "Apex_Class__c": "LeadValidationHandler",
              "Order__c": "10",
              "Object__c": "Lead",
              "Trigger_Event__c": "BeforeInsert"
            }
          }
        ]
    """
    path = root / "_tooling" / "cmt_records.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        CMTRecord(
            cmt_type=str(item["cmt_type"]),
            developer_name=str(item["developer_name"]),
            fields={str(k): str(v) for k, v in item.get("fields", {}).items()},
        )
        for item in raw
    ]
