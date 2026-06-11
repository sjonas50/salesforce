"""SfCliPullClient contract — manifest build, layout detection, source rewrite.

These exercise the full retrieve→parse→rewrite path with an injected fake
runner, so no live org or ``sf`` binary is required. The runner materializes a
source-format tree by copying the sample org fixture into the project the way a
real ``sf project retrieve start`` would.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from offramp.core.models import CategoryName
from offramp.extract.pull.sf_cli import (
    RetrieveResult,
    SfCliError,
    SfCliPullClient,
    _build_package_xml,
    _md_types_for,
)

FIXTURE = Path(__file__).resolve().parents[1] / "integration" / "fixtures" / "sample_org"


def _fake_runner_copying(fixture: Path):
    """Runner that drops the fixture tree into force-app/main/default, like a retrieve."""

    async def runner(argv: list[str], cwd: Path) -> RetrieveResult:
        dest = cwd / "force-app" / "main" / "default"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture, dest)
        return RetrieveResult(returncode=0, stdout='{"status":0}', stderr="")

    return runner


def test_package_xml_dedupes_and_sorts_types() -> None:
    types = _md_types_for({CategoryName.RECORD_TRIGGERED_FLOW, CategoryName.SCREEN_FLOW})
    assert types == {"Flow"}  # both Flow variants collapse to one Metadata type
    xml = _build_package_xml(types, "66.0")
    assert xml.count("<name>Flow</name>") == 1
    assert "<version>66.0</version>" in xml
    assert "*" in xml


def test_md_types_omits_cdc() -> None:
    # CHANGE_DATA_CAPTURE has no Metadata API type — must not appear in the manifest.
    assert _md_types_for({CategoryName.CHANGE_DATA_CAPTURE}) == set()


@pytest.mark.asyncio
async def test_pull_parses_retrieved_source_and_rewrites_provenance() -> None:
    client = SfCliPullClient(org_alias="fisher", _runner=_fake_runner_copying(FIXTURE))
    records = list(await client.pull())

    assert records, "expected the sample org to yield components"
    # Every record must carry sf_cli provenance, not the fixture parser's default.
    assert {r.source for r in records} == {"sf_cli"}
    # A known flow from the fixture comes through.
    assert any(r.category.value.endswith("flow") for r in records)


@pytest.mark.asyncio
async def test_pull_raises_on_nonzero_exit() -> None:
    async def failing(argv: list[str], cwd: Path) -> RetrieveResult:
        return RetrieveResult(returncode=1, stdout="", stderr='{"message":"No authorization"}')

    client = SfCliPullClient(org_alias="fisher", _runner=failing)
    with pytest.raises(SfCliError, match="No authorization"):
        await client.pull()


@pytest.mark.asyncio
async def test_pull_raises_when_no_source_root_found() -> None:
    async def empty_success(argv: list[str], cwd: Path) -> RetrieveResult:
        return RetrieveResult(returncode=0, stdout='{"status":0}', stderr="")

    client = SfCliPullClient(org_alias="fisher", _runner=empty_success)
    with pytest.raises(SfCliError, match="no source-format metadata"):
        await client.pull()


@pytest.mark.asyncio
async def test_category_filter_limits_manifest_and_records() -> None:
    captured: dict[str, str] = {}

    async def capturing(argv: list[str], cwd: Path) -> RetrieveResult:
        captured["manifest"] = (cwd / "package.xml").read_text()
        dest = cwd / "force-app" / "main" / "default"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE, dest)
        return RetrieveResult(returncode=0, stdout="", stderr="")

    client = SfCliPullClient(org_alias="fisher", _runner=capturing)
    await client.pull(categories=[CategoryName.APEX_CLASS])

    assert "<name>ApexClass</name>" in captured["manifest"]
    assert "<name>Flow</name>" not in captured["manifest"]
