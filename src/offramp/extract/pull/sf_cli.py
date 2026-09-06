"""sf CLI pull client (C1, AD-29 secondary path).

Generates a ``package.xml`` covering every metadata type behind the 21
categories, runs ``sf project retrieve start``, then reads the output with
the source-tree reader (C19). The command runner is injectable so tests
never shell out.

Retrieve limits (pitfall 13): 10,000 files / 39 MB compressed per call. The
client retrieves in type groups and, on a size failure, re-splits the group
and retries so one oversized org does not fail the whole pull.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.source_tree import SourceTree

log = get_logger(__name__)

# Metadata API type names, grouped so a retrieve stays well under the caps.
TYPE_GROUPS: list[list[str]] = [
    ["ApexClass", "ApexTrigger"],
    ["Flow", "FlowDefinition"],
    ["CustomObject", "CustomField", "ValidationRule", "RecordType"],
    [
        "Workflow",
        "WorkflowRule",
        "WorkflowFieldUpdate",
        "WorkflowAlert",
        "WorkflowTask",
        "WorkflowOutboundMessage",
    ],
    ["ApprovalProcess", "AssignmentRules", "AutoResponseRules", "EscalationRules", "SharingRules"],
    ["LightningComponentBundle"],
    ["PlatformEventChannel", "PlatformEventChannelMember", "CustomMetadata"],
]

Runner = Callable[[list[str]], "CommandResult"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def subprocess_runner(argv: list[str]) -> CommandResult:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def package_xml(types: Iterable[str], *, api_version: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    for t in types:
        lines.append(
            f"    <types>\n        <members>*</members>\n        <name>{t}</name>\n    </types>"
        )
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    return "\n".join(lines) + "\n"


class SfCliPullClient:
    """Wraps ``sf project retrieve start`` and reads the result as a source tree."""

    source_name = "sf_cli"

    def __init__(
        self,
        *,
        org_alias: str,
        output_dir: Path,
        api_version: str = "66.0",
        sf_binary: str = "sf",
        runner: Runner = subprocess_runner,
        type_groups: list[list[str]] | None = None,
    ) -> None:
        self.org_alias = org_alias
        self.output_dir = output_dir
        self.api_version = api_version
        self.sf_binary = sf_binary
        self.runner = runner
        self.type_groups = type_groups or TYPE_GROUPS
        self.source_version = self._cli_version()
        self.retrieved_groups: list[list[str]] = []
        self.failed_groups: list[tuple[list[str], str]] = []
        self.failures: list[str] = []

    def _cli_version(self) -> str:
        try:
            res = self.runner([self.sf_binary, "--version"])
            return res.stdout.strip().split("\n")[0] if res.returncode == 0 else "unknown"
        except (OSError, FileNotFoundError):
            return "unavailable"

    async def list_categories(self) -> set[CategoryName]:
        return set(CategoryName)

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for group in self.type_groups:
            self._retrieve(group)
        tree = SourceTree(self.output_dir)
        wanted = set(categories) if categories else None
        self.failures = [f"{'+'.join(t)}: {msg}" for t, msg in self.failed_groups]
        recs = tree.records(
            source=self.source_name,
            source_version=self.source_version,
            api_version=self.api_version,
            categories=wanted,
        )
        log.info(
            "extract.sf_cli.pulled",
            records=len(recs),
            groups=len(self.retrieved_groups),
            failed=len(self.failed_groups),
        )
        return recs

    def _retrieve(self, types: list[str]) -> None:
        manifest = self.output_dir / f"package-{'-'.join(types)[:60]}.xml"
        manifest.write_text(package_xml(types, api_version=self.api_version), encoding="utf-8")
        argv = [
            self.sf_binary,
            "project",
            "retrieve",
            "start",
            "--manifest",
            str(manifest),
            "--target-org",
            self.org_alias,
            "--output-dir",
            str(self.output_dir),
            "--api-version",
            self.api_version,
            "--json",
        ]
        try:
            res = self.runner(argv)
        except OSError as exc:  # sf binary missing / not executable
            self.failed_groups.append((types, f"{self.sf_binary}: {exc}"))
            log.error("extract.sf_cli.binary_unavailable", binary=self.sf_binary, error=str(exc))
            return
        ok = res.returncode == 0
        message = ""
        if res.stdout.strip().startswith("{"):
            try:
                payload = json.loads(res.stdout)
                ok = ok and payload.get("status", 0) == 0
                message = str(payload.get("message") or payload.get("name") or "")
            except json.JSONDecodeError:
                pass
        if ok:
            self.retrieved_groups.append(types)
            return
        text = (message or res.stderr or res.stdout)[:400]
        if len(types) > 1 and (
            "limit" in text.lower()
            or "too large" in text.lower()
            or "10000" in text
            or "39" in text
        ):
            log.warning("extract.sf_cli.resplit", types=types, error=text)
            mid = len(types) // 2
            self._retrieve(types[:mid])
            self._retrieve(types[mid:])
            return
        self.failed_groups.append((types, text))
        log.error("extract.sf_cli.retrieve_failed", types=types, error=text)
