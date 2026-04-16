"""Fixture-backed pull client.

Reads a directory tree shaped like a real ``sf project retrieve start``
output and emits ``RawMetadataRecord`` instances. Used by integration tests
and ``make smoke`` until a real scratch org is provisioned.

Layout expected under ``root/``::

    root/
    ├── flows/                       *.flow-meta.xml
    ├── classes/                     *.cls + *.cls-meta.xml
    ├── triggers/                    *.trigger + *.trigger-meta.xml
    ├── objects/<Object>/
    │   ├── validationRules/         *.validationRule-meta.xml
    │   ├── fields/                  *.field-meta.xml (formulas)
    │   └── ...
    ├── workflows/                   *.workflow-meta.xml
    ├── approvalProcesses/           *.approvalProcess-meta.xml
    ├── lwc/<bundle>/                .js + .html + .css + .js-meta.xml
    ├── platformEvents/              *.object-meta.xml (with eventType)
    └── _tooling/                    JSON dumps of Tooling API queries
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import PullClient, RawMetadataRecord

log = get_logger(__name__)


# Glob → CategoryName mapping. The pull layer is intentionally dumb here:
# anything that matches a glob becomes a record of the corresponding category;
# semantic interpretation is the per-category extractor's job.
_CATEGORY_GLOBS: dict[CategoryName, list[str]] = {
    CategoryName.RECORD_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.SCREEN_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.SCHEDULE_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.AUTOLAUNCHED_FLOW: ["flows/*.flow-meta.xml"],
    CategoryName.FLOW_ORCHESTRATION: ["flows/*.flow-meta.xml"],
    CategoryName.PROCESS_BUILDER: ["flows/*.flow-meta.xml"],
    CategoryName.APEX_TRIGGER: ["triggers/*.trigger-meta.xml"],
    CategoryName.APEX_CLASS: ["classes/*.cls-meta.xml"],
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
}


class FixturePullClient:
    """Reads a fixture org dump from disk."""

    source_name = "fixture"

    def __init__(
        self,
        root: Path,
        *,
        version: str = "0.1.0",
        api_version: str = "66.0",
    ) -> None:
        self.root = root
        self.source_version = version
        self.api_version = api_version

    async def list_categories(self) -> set[CategoryName]:
        """Categories with at least one matching file under ``root``."""
        present: set[CategoryName] = set()
        for cat, globs in _CATEGORY_GLOBS.items():
            for g in globs:
                if any(self.root.glob(g)):
                    present.add(cat)
                    break
        return present

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        wanted = set(categories) if categories else set(CategoryName)
        records: list[RawMetadataRecord] = []
        # Track which (path, api_name) pairs we've already emitted so a single
        # XML file ends up classified as exactly one category — even when
        # multiple Flow-variant globs would match it.
        emitted_paths: set[str] = set()
        for cat in _category_priority_order(wanted):
            for rec in self._pull_category(cat):
                key = rec.payload.get("path", rec.api_name)
                if key in emitted_paths:
                    continue
                emitted_paths.add(str(key))
                records.append(rec)
        return records

    def _pull_category(self, category: CategoryName) -> Iterator[RawMetadataRecord]:
        for glob in _CATEGORY_GLOBS.get(category, []):
            for path in sorted(self.root.glob(glob)):
                # LWC bundles are directories; everything else is a file.
                if category is CategoryName.LWC_BUNDLE:
                    if not path.is_dir():
                        continue
                    payload = self._lwc_bundle_payload(path)
                    api_name = path.name
                else:
                    if not path.is_file():
                        continue
                    payload = self._file_payload(path)
                    api_name = self._derive_api_name(path)
                # Some globs match multiple categories (e.g. all *.flow-meta.xml
                # files match every Flow variant). Filter by payload signal where
                # possible so we don't emit duplicate records for the wrong type.
                if not self._matches_category(category, payload):
                    continue
                yield RawMetadataRecord(
                    source=self.source_name,
                    source_version=self.source_version,
                    api_version=self.api_version,
                    category=category,
                    api_name=api_name,
                    payload=payload,
                )

    def _file_payload(self, path: Path) -> dict[str, Any]:
        """Return raw text + a few derived fields. Real parsing is per-category."""
        return {
            "path": str(path.relative_to(self.root)),
            "raw_xml": path.read_text(encoding="utf-8"),
        }

    def _lwc_bundle_payload(self, path: Path) -> dict[str, Any]:
        bundle: dict[str, Any] = {
            "path": str(path.relative_to(self.root)),
            "files": {},
        }
        for fp in sorted(path.iterdir()):
            if fp.is_file():
                bundle["files"][fp.name] = fp.read_text(encoding="utf-8")
        return bundle

    @staticmethod
    def _derive_api_name(path: Path) -> str:
        """Strip Salesforce metadata suffixes to recover the developer name."""
        stem = path.name
        # e.g. AccountValidation.validationRule-meta.xml → AccountValidation
        # e.g. LeadHandler.cls-meta.xml → LeadHandler
        return re.sub(r"\.[\w-]+\.xml$|\.cls(-meta\.xml)?$|\.trigger(-meta\.xml)?$", "", stem)

    @staticmethod
    def _matches_category(category: CategoryName, payload: dict[str, Any]) -> bool:
        """Heuristic discriminator for files that match multiple category globs.

        Used for Flow variants and Roll-Up Summary vs Formula Field.
        """
        xml = payload.get("raw_xml", "")
        if category is CategoryName.RECORD_TRIGGERED_FLOW:
            return "<processType>RecordTriggered" in xml or "<triggerType>Record" in xml
        if category is CategoryName.SCREEN_FLOW:
            return "<processType>Flow</processType>" in xml and "<screens>" in xml
        if category is CategoryName.SCHEDULE_TRIGGERED_FLOW:
            return "<processType>ScheduleTriggered" in xml
        if category is CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW:
            return "<triggerType>PlatformEvent" in xml
        if category is CategoryName.AUTOLAUNCHED_FLOW:
            return "<processType>AutoLaunchedFlow</processType>" in xml and "<screens>" not in xml
        if category is CategoryName.FLOW_ORCHESTRATION:
            return "<processType>Orchestrator</processType>" in xml
        if category is CategoryName.PROCESS_BUILDER:
            return "<processType>Workflow</processType>" in xml
        if category is CategoryName.FORMULA_FIELD:
            return "<formula>" in xml and "<summaryOperation>" not in xml
        if category is CategoryName.ROLLUP_SUMMARY:
            return "<summaryOperation>" in xml
        if category is CategoryName.PLATFORM_EVENT:
            return "<eventType>" in xml or "<deploymentStatus>" in xml
        return True


def _category_priority_order(wanted: set[CategoryName]) -> list[CategoryName]:
    """Categories ordered so the more specific Flow variants are tried first.

    The order matters because a single .flow-meta.xml may pattern-match
    multiple categories — e.g. an after-save record-triggered Flow has both
    ``processType=AutoLaunchedFlow`` AND ``triggerType=RecordAfterSave``. The
    fixture pull client iterates in this order and assigns each file to the
    first matching category.
    """
    priority = [
        CategoryName.RECORD_TRIGGERED_FLOW,
        CategoryName.SCHEDULE_TRIGGERED_FLOW,
        CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
        CategoryName.SCREEN_FLOW,
        CategoryName.FLOW_ORCHESTRATION,
        CategoryName.PROCESS_BUILDER,
        CategoryName.AUTOLAUNCHED_FLOW,
        CategoryName.ROLLUP_SUMMARY,
        CategoryName.FORMULA_FIELD,
        CategoryName.PLATFORM_EVENT,
    ]
    seen = set(priority)
    rest = sorted(c for c in wanted if c not in seen)
    return [c for c in priority if c in wanted] + rest


def _ensure_protocol(client: PullClient) -> PullClient:
    """Static-time check that the fixture client satisfies the Protocol."""
    return client


_ = _ensure_protocol  # silence unused-import on the Protocol when type-checked
