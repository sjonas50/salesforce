"""Source tree reader (C19).

One reader for every directory shaped like ``sf project retrieve start``
output: test fixtures, sf CLI retrieves, and customer-supplied SFDX projects.
Emits :class:`RawMetadataRecord` instances for the 21 automation categories
and exposes the schema files (objects, fields, record types) for the schema
extractor (C21).

Layout accepted under ``root`` (a ``force-app/main/default`` prefix is
located automatically)::

    classes/            *.cls + *.cls-meta.xml          (body captured)
    triggers/           *.trigger + *.trigger-meta.xml  (body captured)
    flows/              *.flow-meta.xml
    objects/<Object>/   <Object>.object-meta.xml, fields/, validationRules/, recordTypes/
    workflows/          *.workflow-meta.xml
    approvalProcesses/  *.approvalProcess-meta.xml
    assignmentRules/ autoResponseRules/ escalationRules/ sharingRules/
    lwc/<bundle>/       .js .html .css .js-meta.xml
    _tooling/           JSON dumps (cmt_records.json, cdc_subscriptions.json,
                        dependencies.json, cron_triggers.json)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord

log = get_logger(__name__)

_KNOWN_DIRS = (
    "classes",
    "triggers",
    "flows",
    "objects",
    "workflows",
    "approvalProcesses",
    "assignmentRules",
    "autoResponseRules",
    "escalationRules",
    "sharingRules",
    "lwc",
    "layouts",
    "flexipages",
    "permissionsets",
    "profiles",
    "reports",
)

# Glob → CategoryName. Interpretation is the per-category extractor's job;
# the reader only classifies files and captures their text.
CATEGORY_GLOBS: dict[CategoryName, list[str]] = {
    CategoryName.RECORD_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.SCREEN_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.SCHEDULE_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.AUTOLAUNCHED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.FLOW_ORCHESTRATION: ["flows/*.flow-meta.xml"],
    CategoryName.PROCESS_BUILDER: ["flows/*.flow-meta.xml"],
    CategoryName.APEX_TRIGGER: ["triggers/*.trigger-meta.xml", "triggers/*.trigger"],
    CategoryName.APEX_CLASS: ["classes/*.cls-meta.xml", "classes/*.cls"],
    CategoryName.VALIDATION_RULE: ["objects/*/validationRules/*.validationRule-meta.xml"],
    CategoryName.FORMULA_FIELD: ["objects/*/fields/*.field-meta.xml"],
    CategoryName.WORKFLOW_RULE: ["workflows/*.workflow-meta.xml"],
    CategoryName.APPROVAL_PROCESS: ["approvalProcesses/*.approvalProcess-meta.xml"],
    CategoryName.ASSIGNMENT_RULE: ["assignmentRules/*.assignmentRules-meta.xml"],
    CategoryName.AUTO_RESPONSE_RULE: ["autoResponseRules/*.autoResponseRules-meta.xml"],
    CategoryName.ESCALATION_RULE: ["escalationRules/*.escalationRules-meta.xml"],
    CategoryName.SHARING_RULE: ["sharingRules/*.sharingRules-meta.xml"],
    CategoryName.ROLLUP_SUMMARY: ["objects/*/fields/*.field-meta.xml"],
    CategoryName.PLATFORM_EVENT: ["objects/*__e/*.object-meta.xml"],
    CategoryName.CHANGE_DATA_CAPTURE: ["_tooling/cdc_subscriptions.json"],
    CategoryName.LWC_BUNDLE: ["lwc/*/"],
    CategoryName.PAGE_LAYOUT: ["layouts/*.layout-meta.xml"],
    CategoryName.FLEXIPAGE: ["flexipages/*.flexipage-meta.xml"],
    CategoryName.PERMISSION_SET: ["permissionsets/*.permissionset-meta.xml"],
    CategoryName.PROFILE: ["profiles/*.profile-meta.xml"],
    CategoryName.REPORT: ["reports/**/*.report-meta.xml"],
}

# Flow variants and field kinds share globs; try the most specific first.
_PRIORITY = [
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.PROCESS_BUILDER,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.RECORD_TRIGGERED_FLOW,
    CategoryName.SCREEN_FLOW,
    CategoryName.AUTOLAUNCHED_FLOW,
    CategoryName.ROLLUP_SUMMARY,
    CategoryName.FORMULA_FIELD,
    CategoryName.PLATFORM_EVENT,
]


@dataclass
class ObjectFiles:
    """Every file under ``objects/<Object>/`` that the schema extractor needs."""

    name: str
    object_xml: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # field api name -> xml
    record_types: dict[str, str] = field(default_factory=dict)
    validation_rules: dict[str, str] = field(default_factory=dict)


def locate_source_root(root: Path) -> Path:
    """Return the directory that directly contains ``classes/``, ``objects/``, etc.

    Accepts the directory itself, an SFDX project root (``force-app/main/default``),
    or any ancestor of one of those.
    """
    if any((root / d).is_dir() for d in _KNOWN_DIRS):
        return root
    candidates = sorted(
        {p.parent for d in _KNOWN_DIRS for p in root.glob(f"**/{d}") if p.is_dir()},
        key=lambda p: (len(p.parts), str(p)),
    )
    for c in candidates:
        if "node_modules" in c.parts or ".sfdx" in c.parts:
            continue
        return c
    return root


class SourceTree:
    """Read an sf-retrieve-shaped directory into raw records + schema files."""

    def __init__(self, root: Path) -> None:
        self.given_root = root
        self.root = locate_source_root(root)
        self._text_cache: dict[str, str] = {}

    # ---- automation categories ------------------------------------------------

    def present_categories(self) -> set[CategoryName]:
        present: set[CategoryName] = set()
        for cat in _ordered(set(CategoryName)):
            if any(True for _ in self._iter_category_paths(cat)):
                present.add(cat)
        return present

    def records(
        self,
        *,
        source: str,
        source_version: str,
        api_version: str,
        categories: set[CategoryName] | None = None,
    ) -> list[RawMetadataRecord]:
        wanted = categories or set(CategoryName)
        out: list[RawMetadataRecord] = []
        emitted: set[str] = set()
        for cat in _ordered(wanted):
            for _path, payload, api_name in self._iter_category(cat):
                key = str(payload.get("path", api_name))
                if key in emitted:
                    continue
                emitted.add(key)
                out.append(
                    RawMetadataRecord(
                        source=source,
                        source_version=source_version,
                        api_version=api_version,
                        category=cat,
                        api_name=api_name,
                        payload=payload,
                    )
                )
        return out

    def _iter_category_paths(self, cat: CategoryName) -> Iterator[Path]:
        for glob in CATEGORY_GLOBS.get(cat, []):
            for path in sorted(self.root.glob(glob)):
                if cat is CategoryName.LWC_BUNDLE:
                    if path.is_dir():
                        yield path
                elif path.is_file():
                    yield path

    def _iter_category(self, cat: CategoryName) -> Iterator[tuple[Path, dict[str, Any], str]]:
        seen_names: set[str] = set()
        for path in self._iter_category_paths(cat):
            if cat is CategoryName.LWC_BUNDLE:
                payload = self._lwc_bundle_payload(path)
                api_name = path.name
            elif cat in {CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER}:
                api_name = derive_api_name(path)
                if api_name in seen_names:
                    continue
                payload = self._apex_payload(path, cat)
            else:
                payload = self._file_payload(path)
                api_name = derive_api_name(path)
                if cat is CategoryName.REPORT:
                    # Reports are addressed as Folder/Name.
                    rel = Path(self._rel(path))
                    api_name = str(rel.relative_to("reports").with_name(api_name))
            if not _matches_category(cat, payload):
                continue
            seen_names.add(api_name)
            yield path, payload, api_name

    def _read(self, path: Path) -> str:
        """File text, cached: Flow files are matched by seven category globs."""
        key = str(path)
        if key not in self._text_cache:
            self._text_cache[key] = path.read_text(encoding="utf-8")
        return self._text_cache[key]

    def _file_payload(self, path: Path) -> dict[str, Any]:
        text = self._read(path)
        payload: dict[str, Any] = {"path": self._rel(path)}
        if path.suffix == ".json":
            payload["raw_json"] = text
            payload["raw_xml"] = text  # interface uniformity for older consumers
        else:
            payload["raw_xml"] = text
        obj = _object_from_path(payload["path"])
        if obj:
            payload["object_from_path"] = obj
        return payload

    def _apex_payload(self, path: Path, cat: CategoryName) -> dict[str, Any]:
        """Pair ``X.cls`` with ``X.cls-meta.xml`` (either may be the matched path)."""
        ext = ".cls" if cat is CategoryName.APEX_CLASS else ".trigger"
        stem = derive_api_name(path)
        body_path = path.parent / f"{stem}{ext}"
        meta_path = path.parent / f"{stem}{ext}-meta.xml"
        payload: dict[str, Any] = {
            "path": self._rel(meta_path if meta_path.is_file() else body_path),
        }
        if meta_path.is_file():
            payload["raw_xml"] = meta_path.read_text(encoding="utf-8")
        else:
            payload["raw_xml"] = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f"<{'ApexClass' if ext == '.cls' else 'ApexTrigger'} "
                'xmlns="http://soap.sforce.com/2006/04/metadata">'
                "<status>Active</status></"
                f"{'ApexClass' if ext == '.cls' else 'ApexTrigger'}>"
            )
        if body_path.is_file():
            body = body_path.read_text(encoding="utf-8")
            payload["body" if ext == ".cls" else "trigger_body"] = body
            payload["body_path"] = self._rel(body_path)
        return payload

    def _lwc_bundle_payload(self, path: Path) -> dict[str, Any]:
        bundle: dict[str, Any] = {"path": self._rel(path), "files": {}}
        for fp in sorted(path.iterdir()):
            if fp.is_file():
                bundle["files"][fp.name] = fp.read_text(encoding="utf-8")
        return bundle

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # ---- schema ---------------------------------------------------------------

    def object_files(self) -> list[ObjectFiles]:
        """Everything under ``objects/`` grouped per sObject."""
        objects_dir = self.root / "objects"
        if not objects_dir.is_dir():
            return []
        out: list[ObjectFiles] = []
        for obj_dir in sorted(p for p in objects_dir.iterdir() if p.is_dir()):
            of = ObjectFiles(name=obj_dir.name)
            meta = obj_dir / f"{obj_dir.name}.object-meta.xml"
            if meta.is_file():
                of.object_xml = meta.read_text(encoding="utf-8")
            for fp in sorted((obj_dir / "fields").glob("*.field-meta.xml")):
                of.fields[derive_api_name(fp)] = fp.read_text(encoding="utf-8")
            for fp in sorted((obj_dir / "recordTypes").glob("*.recordType-meta.xml")):
                of.record_types[derive_api_name(fp)] = fp.read_text(encoding="utf-8")
            for fp in sorted((obj_dir / "validationRules").glob("*.validationRule-meta.xml")):
                of.validation_rules[derive_api_name(fp)] = fp.read_text(encoding="utf-8")
            out.append(of)
        return out

    # ---- tooling dumps --------------------------------------------------------

    def tooling_json(self, name: str) -> Any:
        """Load ``_tooling/<name>.json`` if present, else ``None``."""
        p = self.root / "_tooling" / f"{name}.json"
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


_SUFFIX_RE = re.compile(
    r"\.[\w-]+\.xml$|\.cls(-meta\.xml)?$|\.trigger(-meta\.xml)?$|\.json$",
)


def derive_api_name(path: Path) -> str:
    """Strip Salesforce metadata suffixes to recover the developer name.

    ``AccountValidation.validationRule-meta.xml`` → ``AccountValidation``;
    ``LeadHandler.cls`` → ``LeadHandler``;
    ``Opportunity.HighValueDiscount.approvalProcess-meta.xml`` →
    ``Opportunity.HighValueDiscount``.
    """
    return _SUFFIX_RE.sub("", path.name)


def _object_from_path(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if "objects" in parts:
        i = parts.index("objects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _ordered(wanted: set[CategoryName]) -> list[CategoryName]:
    seen = set(_PRIORITY)
    rest = sorted(c for c in wanted if c not in seen)
    return [c for c in _PRIORITY if c in wanted] + rest


def _matches_category(category: CategoryName, payload: dict[str, Any]) -> bool:
    """Discriminate files that match several globs (Flow variants, field kinds)."""
    xml = payload.get("raw_xml", "")
    if category is CategoryName.RECORD_TRIGGERED_FLOW:
        return (
            "<triggerType>Record" in xml
            and "<processType>Orchestrator" not in xml
            and "<processType>Workflow" not in xml
        )
    if category is CategoryName.SCREEN_FLOW:
        return "<processType>Flow</processType>" in xml and "<screens>" in xml
    if category is CategoryName.SCHEDULE_TRIGGERED_FLOW:
        return "<triggerType>Scheduled" in xml or "<processType>ScheduleTriggered" in xml
    if category is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW:
        return "<triggerType>PlatformEvent" in xml
    if category is CategoryName.AUTOLAUNCHED_FLOW:
        return (
            "<processType>AutoLaunchedFlow</processType>" in xml
            or "<processType>Flow</processType>" in xml
        ) and "<screens>" not in xml
    if category is CategoryName.FLOW_ORCHESTRATION:
        return "<processType>Orchestrator</processType>" in xml
    if category is CategoryName.PROCESS_BUILDER:
        return (
            "<processType>Workflow</processType>" in xml
            or "<processType>InvocableProcess</processType>" in xml
            or "<processType>CustomEvent</processType>" in xml
        )
    if category is CategoryName.FORMULA_FIELD:
        return "<formula>" in xml and "<summaryOperation>" not in xml
    if category is CategoryName.ROLLUP_SUMMARY:
        return "<summaryOperation>" in xml
    if category is CategoryName.PLATFORM_EVENT:
        return "<eventType>" in xml
    return True
