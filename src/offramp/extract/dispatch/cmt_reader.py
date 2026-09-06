"""Custom Metadata Type record reader.

Trigger Actions Framework / metadata-driven dispatch patterns store the real
handler graph in CMT records (e.g. ``Trigger_Action__mdt`` rows). Static
analysis of the trigger code returns a single dispatcher class; this reader
recovers the full handler list by querying the CMT records themselves.

Two sources feed the same :class:`CMTRecord` shape: a ``_tooling/cmt_records.json``
dump on the directory path, and
:meth:`offramp.extract.pull.tooling_api.ToolingApiPullClient.cmt_records` on the
REST path.
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


def read_cmt_records_from_source(roots: list[Path]) -> list[CMTRecord]:
    """Load CMT rows from source format: ``customMetadata/<Type>.<Record>.md-meta.xml``.

    Each file is one record::

        <CustomMetadata xmlns="http://soap.sforce.com/2006/04/metadata" ...>
            <label>Contact Customer Fields</label>
            <values><field>Customer_City__c</field><value xsi:type="xsd:string">MailingCity</value></values>

    ``Customer_Fields.Contact_Customer_Fields.md-meta.xml`` is record
    ``Contact_Customer_Fields`` of ``Customer_Fields__mdt``.
    """
    import xml.etree.ElementTree as ET

    out: list[CMTRecord] = []
    for root in roots:
        d = root / "customMetadata"
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md-meta.xml")):
            stem = path.name[: -len(".md-meta.xml")]
            if "." not in stem:
                continue
            type_dev, dev = stem.split(".", 1)
            try:
                tree = ET.parse(path)
            except ET.ParseError:
                continue
            fields: dict[str, str] = {}
            for v in tree.getroot():
                if not v.tag.endswith("}values"):
                    continue
                fname = value = None
                for child in v:
                    if child.tag.endswith("}field"):
                        fname = child.text
                    elif child.tag.endswith("}value"):
                        value = child.text
                if fname:
                    fields[fname] = value or ""
            out.append(CMTRecord(cmt_type=f"{type_dev}__mdt", developer_name=dev, fields=fields))
    return out


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
