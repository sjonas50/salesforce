from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from offramp.core.models import CategoryName
from offramp.extract.pull.sf_cli import CommandResult, SfCliPullClient, package_xml

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def test_package_xml_lists_types() -> None:
    xml = package_xml(["ApexClass", "Flow"], api_version="66.0")
    assert (
        "<name>ApexClass</name>" in xml
        and "<name>Flow</name>" in xml
        and "<version>66.0</version>" in xml
    )


@pytest.mark.asyncio
async def test_retrieve_with_fake_runner_and_resplit(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[1:2] == ["--version"] or argv[-1] == "--version":
            return CommandResult(0, "@salesforce/cli/2.60.0", "")
        manifest = Path(argv[argv.index("--manifest") + 1]).read_text()
        # Simulate the 10,000-file cap on the combined object group, forcing a re-split.
        if "CustomObject" in manifest and "RecordType" in manifest:
            return CommandResult(
                1,
                json.dumps({"status": 1, "message": "LIMIT_EXCEEDED: too many files (10000)"}),
                "",
            )
        # "Retrieve" by copying the fixture tree into the output dir once.
        out = Path(argv[argv.index("--output-dir") + 1])
        if not (out / "classes").exists():
            shutil.copytree(FIX, out, dirs_exist_ok=True)
        return CommandResult(0, json.dumps({"status": 0, "result": {}}), "")

    client = SfCliPullClient(org_alias="scratch", output_dir=tmp_path / "retrieve", runner=runner)
    recs = list(
        await client.pull(categories={CategoryName.APEX_CLASS, CategoryName.RECORD_TRIGGERED_FLOW})
    )
    assert client.source_version.startswith("@salesforce/cli")
    assert any(r.category is CategoryName.APEX_CLASS and r.payload.get("body") for r in recs)
    assert any(r.category is CategoryName.RECORD_TRIGGERED_FLOW for r in recs)
    # The oversized group was split and retried rather than failed.
    assert not client.failed_groups
    manifests = [
        Path(a[a.index("--manifest") + 1]).read_text()  # noqa: ASYNC240 — test inspection only
        for a in calls
        if "--manifest" in a
    ]
    assert any("CustomObject" in m and "RecordType" not in m for m in manifests)
