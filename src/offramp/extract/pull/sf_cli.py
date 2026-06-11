"""sf CLI pull client (Phase 1.2) — real metadata retrieval from a live org.

Strategy
--------
``sf project retrieve start`` emits **source format** (e.g.
``flows/My.flow-meta.xml``), which is byte-for-byte the layout the
:class:`~offramp.extract.pull.fixture.FixturePullClient` already knows how to
parse. So this client is a thin shell around three steps:

1. Build a ``package.xml`` manifest covering the requested categories.
2. Shell out to ``sf project retrieve start`` into a temporary SFDX project.
3. Re-use the fixture parser over the retrieved tree, rewriting each record's
   ``source`` to ``sf_cli`` so the reconciler applies the right precedence.

The subprocess runner is injectable (``_runner``) so the manifest-build →
layout-detect → parse → rewrite path is unit-testable without a live org or
the ``sf`` binary installed.

Auth is whatever the ``sf`` CLI already holds for ``org_alias`` (typically the
JWT bearer flow, surfaced to the CLI via ``sf org login jwt``). This client
does not manage credentials itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.fixture import FixturePullClient

log = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class RetrieveResult:
    """Outcome of a ``sf project retrieve start`` invocation."""

    returncode: int
    stdout: str
    stderr: str


# Runner contract: given the argv and working directory, run it and return the
# result. The default runner shells out; tests inject a fake that materializes
# the expected source tree instead.
Runner = Callable[[list[str], Path], Awaitable[RetrieveResult]]


# CategoryName → Metadata API type name(s) for the manifest. Several categories
# map to the same Metadata type (all Flow variants → ``Flow``); the manifest
# de-dupes. CHANGE_DATA_CAPTURE is intentionally absent — it is not exposed via
# the Metadata API and is the Tooling client's responsibility.
_CATEGORY_TO_MD_TYPES: dict[CategoryName, tuple[str, ...]] = {
    CategoryName.RECORD_TRIGGERED_FLOW: ("Flow",),
    CategoryName.SCREEN_FLOW: ("Flow",),
    CategoryName.SCHEDULE_TRIGGERED_FLOW: ("Flow",),
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: ("Flow",),
    CategoryName.AUTOLAUNCHED_FLOW: ("Flow",),
    CategoryName.FLOW_ORCHESTRATION: ("Flow",),
    CategoryName.PROCESS_BUILDER: ("Flow",),
    CategoryName.APEX_TRIGGER: ("ApexTrigger",),
    CategoryName.APEX_CLASS: ("ApexClass",),
    CategoryName.VALIDATION_RULE: ("ValidationRule",),
    CategoryName.FORMULA_FIELD: ("CustomField",),
    CategoryName.ROLLUP_SUMMARY: ("CustomField",),
    CategoryName.WORKFLOW_RULE: ("WorkflowRule",),
    CategoryName.APPROVAL_PROCESS: ("ApprovalProcess",),
    CategoryName.ASSIGNMENT_RULE: ("AssignmentRules",),
    CategoryName.AUTO_RESPONSE_RULE: ("AutoResponseRules",),
    CategoryName.ESCALATION_RULE: ("EscalationRules",),
    CategoryName.SHARING_RULE: ("SharingRules",),
    CategoryName.PLATFORM_EVENT: ("CustomObject",),
    CategoryName.LWC_BUNDLE: ("LightningComponentBundle",),
}

# Subdirectories that mark the source-format metadata root. After a retrieve we
# search the project tree for the first directory containing any of these.
_SOURCE_ROOT_MARKERS = ("flows", "classes", "triggers", "objects", "lwc", "workflows")


class SfCliError(RuntimeError):
    """Raised when the sf CLI is missing, unauthenticated, or fails to retrieve."""


class SfCliPullClient:
    """Wraps ``sf project retrieve start`` against an authenticated org."""

    source_name = "sf_cli"

    def __init__(
        self,
        *,
        org_alias: str,
        sf_binary: str = "sf",
        api_version: str = "66.0",
        timeout_s: float = 600.0,
        _runner: Runner | None = None,
    ) -> None:
        self.org_alias = org_alias
        self.sf_binary = sf_binary
        self.api_version = api_version
        self.timeout_s = timeout_s
        self.source_version = sf_binary
        self._runner: Runner = _runner or self._default_runner

    async def list_categories(self) -> set[CategoryName]:
        """Categories this client can request via the Metadata API.

        Everything except CHANGE_DATA_CAPTURE, which only the Tooling client
        can produce.
        """
        return set(_CATEGORY_TO_MD_TYPES)

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        """Retrieve metadata for the requested categories and parse it.

        Records for unmappable categories (CDC) are silently absent — that is
        the honest result for a Metadata-API-only source.
        """
        wanted = set(categories) if categories else set(CategoryName)
        manifest_types = _md_types_for(wanted)
        if not manifest_types:
            log.info("extract.sf_cli.no_metadata_types", requested=[c.value for c in wanted])
            return []

        project = Path(tempfile.mkdtemp(prefix="offramp-sf-"))
        try:
            _write_sfdx_project(project, self.api_version)
            manifest = project / "package.xml"
            manifest.write_text(_build_package_xml(manifest_types, self.api_version), "utf-8")

            argv = [
                self.sf_binary,
                "project",
                "retrieve",
                "start",
                "--manifest",
                str(manifest),
                "--target-org",
                self.org_alias,
                "--json",
            ]
            log.info(
                "extract.sf_cli.retrieve.start",
                org=self.org_alias,
                md_types=sorted(manifest_types),
            )
            result = await self._runner(argv, project)
            if result.returncode != 0:
                raise SfCliError(
                    f"`sf project retrieve start` exited {result.returncode} for org "
                    f"'{self.org_alias}': {_first_error(result)}"
                )

            md_root = _locate_source_root(project)
            if md_root is None:
                raise SfCliError(
                    f"Retrieve succeeded but no source-format metadata found under {project}. "
                    "Org may have no matching components, or the manifest matched nothing."
                )

            # Re-use the fixture parser, then rewrite provenance to this source.
            parser = FixturePullClient(md_root, api_version=self.api_version)
            parsed = list(await parser.pull(categories=categories))
            records = [
                dataclasses.replace(r, source=self.source_name, source_version=self.source_version)
                for r in parsed
            ]
            log.info("extract.sf_cli.retrieve.done", records=len(records), root=str(md_root))
            return records
        finally:
            shutil.rmtree(project, ignore_errors=True)

    async def _default_runner(self, argv: list[str], cwd: Path) -> RetrieveResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SfCliError(
                f"`{self.sf_binary}` not found on PATH. Install the Salesforce CLI "
                "(`npm install -g @salesforce/cli`) and authenticate the org "
                f"(`sf org login jwt --username <user> --jwt-key-file <pem> "
                f"--client-id <id> --alias {self.org_alias}`)."
            ) from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except TimeoutError as exc:
            proc.kill()
            raise SfCliError(
                f"`sf project retrieve start` timed out after {self.timeout_s:.0f}s"
            ) from exc
        return RetrieveResult(
            returncode=proc.returncode or 0,
            stdout=stdout_b.decode("utf-8", "replace"),
            stderr=stderr_b.decode("utf-8", "replace"),
        )


def _md_types_for(categories: set[CategoryName]) -> set[str]:
    types: set[str] = set()
    for cat in categories:
        types.update(_CATEGORY_TO_MD_TYPES.get(cat, ()))
    return types


def _build_package_xml(md_types: set[str], api_version: str) -> str:
    """Render a wildcard manifest covering every requested Metadata type."""
    types_xml = "\n".join(
        f"    <types>\n        <members>*</members>\n        <name>{name}</name>\n    </types>"
        for name in sorted(md_types)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        f"{types_xml}\n"
        f"    <version>{api_version}</version>\n"
        "</Package>\n"
    )


def _write_sfdx_project(project: Path, api_version: str) -> None:
    """Minimal SFDX project so retrieve lands in source format."""
    (project / "force-app").mkdir(parents=True, exist_ok=True)
    (project / "sfdx-project.json").write_text(
        json.dumps(
            {
                "packageDirectories": [{"path": "force-app", "default": True}],
                "namespace": "",
                "sfdcLoginUrl": "https://login.salesforce.com",
                "sourceApiVersion": api_version,
            },
            indent=2,
        ),
        "utf-8",
    )


def _locate_source_root(project: Path) -> Path | None:
    """Find the directory holding the retrieved source-format metadata.

    Conventionally ``force-app/main/default``, but we search defensively for the
    shallowest directory containing a known marker subdirectory so we are robust
    to package-directory layout differences across sf CLI versions.
    """
    candidate = project / "force-app" / "main" / "default"
    if candidate.is_dir() and _has_marker(candidate):
        return candidate
    best: Path | None = None
    for path in project.rglob("*"):
        if (
            path.is_dir()
            and _has_marker(path)
            and (best is None or len(path.parts) < len(best.parts))
        ):
            best = path
    return best


def _has_marker(path: Path) -> bool:
    return any((path / marker).is_dir() for marker in _SOURCE_ROOT_MARKERS)


def _first_error(result: RetrieveResult) -> str:
    """Pull a human-readable message out of ``--json`` output, fall back to stderr."""
    for blob in (result.stdout, result.stderr):
        blob = blob.strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return blob.splitlines()[-1] if blob else "unknown error"
        if isinstance(data, dict):
            msg = data.get("message") or data.get("name")
            if msg:
                return str(msg)
    return "unknown error (no diagnostic output)"
