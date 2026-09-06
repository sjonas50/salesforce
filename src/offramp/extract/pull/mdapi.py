"""Metadata API pull client (AD-29): full bodies with no CLI on either side.

Uses the SOAP Metadata API ``retrieve`` through the MCP gateway backend
(simple-salesforce's ``mdapi``), unzips the result, and reads it with the
source-tree reader (C19). This is how the REST path gets the approval,
assignment, escalation, auto-response, sharing, report, layout, and
permission-set bodies the Tooling API does not expose.

Retrieves are batched per type group (pitfall 13: 10,000 files / 39 MB per
retrieve); a failing group is re-split once, then recorded as failed.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.source_tree import SourceTree

log = get_logger(__name__)

# Types the Tooling client cannot read in full; the REST path retrieves just these.
PARTIAL_TYPES: list[str] = [
    "ApprovalProcess",
    "AssignmentRules",
    "AutoResponseRules",
    "EscalationRules",
    "SharingRules",
    "Layout",
    "FlexiPage",
    "PermissionSet",
    "Profile",
    "Report",
]
FOLDERED_TYPES = {
    "Report": "ReportFolder",
    "Dashboard": "DashboardFolder",
    "EmailTemplate": "EmailFolder",
    "Document": "DocumentFolder",
}

_TYPE_GROUPS: list[list[str]] = [
    ["ApexClass", "ApexTrigger"],
    ["Flow"],
    ["CustomObject", "CustomField", "ValidationRule", "RecordType"],
    ["Workflow"],
    ["ApprovalProcess", "AssignmentRules", "AutoResponseRules", "EscalationRules", "SharingRules"],
    ["LightningComponentBundle"],
    ["Layout", "FlexiPage"],
    ["PermissionSet", "Profile"],
    ["Report"],
]


class MetadataApiPullClient:
    """``retrieve`` → ZIP → source tree → records."""

    source_name = "metadata_api"

    def __init__(
        self,
        *,
        gateway: Any,
        org_alias: str,
        workdir: Path,
        api_version: str = "66.0",
        types: Iterable[str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.org_alias = org_alias
        self.workdir = workdir
        self.api_version = api_version
        self.source_version = f"mdapi-{api_version}"
        wanted = set(types) if types else None
        self.type_groups = [[t for t in g if wanted is None or t in wanted] for g in _TYPE_GROUPS]
        self.type_groups = [g for g in self.type_groups if g]
        self.retrieved: list[list[str]] = []
        self.failed: list[tuple[list[str], str]] = []
        self.failures: list[str] = []

    async def list_categories(self) -> set[CategoryName]:
        return set(CategoryName)

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        for group in self.type_groups:
            await self._retrieve_group(group)
        self.failures = [f"{'+'.join(t)}: {msg}" for t, msg in self.failed]
        tree = SourceTree(self.workdir)
        wanted = set(categories) if categories else None
        recs = tree.records(
            source=self.source_name,
            source_version=self.source_version,
            api_version=self.api_version,
            categories=wanted,
        )
        log.info(
            "extract.mdapi.pulled",
            records=len(recs),
            groups=len(self.retrieved),
            failed=len(self.failed),
        )
        return recs

    async def _members(self, mtype: str) -> list[str]:
        """``*`` for flat types; ``Folder`` + ``Folder/Name`` for foldered types."""
        folder_type = FOLDERED_TYPES.get(mtype)
        if folder_type is None:
            return ["*"]
        folders = await self.gateway.sf_mdapi_list(folder_type)
        members: list[str] = []
        for folder in folders:
            members.append(folder)
            members.extend(await self.gateway.sf_mdapi_list(mtype, folder=folder))
        return members or ["*"]

    async def _retrieve_group(self, types: list[str], *, allow_split: bool = True) -> None:
        unpackaged = {t: await self._members(t) for t in types}
        try:
            blob = await self.gateway.sf_mdapi_retrieve(unpackaged)
        except Exception as exc:
            text = str(exc)[:300]
            if allow_split and len(types) > 1:
                log.warning("extract.mdapi.resplit", types=types, error=text)
                mid = len(types) // 2
                await self._retrieve_group(types[:mid], allow_split=False)
                await self._retrieve_group(types[mid:], allow_split=False)
                return
            self.failed.append((types, text))
            log.error("extract.mdapi.retrieve_failed", types=types, error=text)
            return
        self._unzip(blob)
        self.retrieved.append(types)

    def _unzip(self, blob: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for member in zf.namelist():
                rel = member.split("/", 1)[1] if member.startswith("unpackaged/") else member
                if not rel or rel.endswith("/") or rel == "package.xml":
                    continue
                # Salesforce percent-encodes member names in the ZIP
                # (``Account-Account %28Marketing%29 Layout.layout``); the Tooling path
                # and every other source name them decoded.
                rel = _source_format_name(unquote(rel))
                target = (self.workdir / rel).resolve()
                if not str(target).startswith(str(self.workdir.resolve())):
                    continue  # zip-slip guard
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))


# Metadata API format names the XML by type suffix (``Lead.assignmentRules``,
# ``Sales_User.permissionset``); the source tree reader expects source format
# (``Lead.assignmentRules-meta.xml``). Content-bearing types (Apex, LWC, email,
# static resources) already share their layout between the two formats.
_META_ONLY_SUFFIXES = (
    ".approvalProcess",
    ".assignmentRules",
    ".autoResponseRules",
    ".escalationRules",
    ".sharingRules",
    ".layout",
    ".flexipage",
    ".permissionset",
    ".profile",
    ".report",
    ".flow",
    ".workflow",
    ".object",
    ".customMetadata",
    ".labels",
    ".queue",
    ".group",
    ".role",
    ".namedCredential",
)


def _source_format_name(rel: str) -> str:
    if rel.endswith("-meta.xml") or rel.endswith(".xml"):
        return rel
    return rel + "-meta.xml" if rel.endswith(_META_ONLY_SUFFIXES) else rel


class CompositePullClient:
    """Concatenate records from several clients; the reconciler merges by precedence."""

    source_name = "composite"

    def __init__(self, *clients: Any) -> None:
        self.clients = list(clients)
        self.source_version = "+".join(getattr(c, "source_version", "?") for c in clients)
        self.api_version = getattr(clients[0], "api_version", "66.0") if clients else "66.0"
        self.failures: list[str] = []

    async def list_categories(self) -> set[CategoryName]:
        out: set[CategoryName] = set()
        for c in self.clients:
            out |= await c.list_categories()
        return out

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        wanted = set(categories) if categories else None
        out: list[RawMetadataRecord] = []
        for c in self.clients:
            out.extend(await c.pull(categories=wanted))
        self.failures = [f for c in self.clients for f in getattr(c, "failures", [])]
        return out
