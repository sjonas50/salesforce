"""Phase 3 e2e: extract → generate → load artifact → execute through OoE runtime.

Proves the full vertical slice for Tier 1 (validation rule):
  1. fixture extractor produces a Component for ``Account.Industry_Required``
  2. tier1 translator emits a Python rule module
  3. orchestrator writes a deployable artifact directory
  4. RulesEngine.load_artifact() loads the registry
  5. OoE runtime executes a save and the validation fires correctly

This is the architectural backbone the v2.1 plan calls for at M7 (week 24).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.extract.ooe_audit.audit import OoEStep
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient
from offramp.generate.orchestrator import GenerateOrchestrator
from offramp.runtime.ooe.state_machine import OoERuntime, ValidationFailedError
from offramp.runtime.rules.engine import load_artifact

FIXTURE = Path(__file__).parent / "fixtures" / "sample_org"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extract_generate_run_validation(tmp_path: Path) -> None:
    out = tmp_path / "artifact"
    engram = InMemoryEngramClient()

    # Step 1+2: extract.
    client = FixturePullClient(FIXTURE)
    ext = ExtractOrchestrator(
        org_alias="sample_org", client=client, engram=engram, fixture_root=FIXTURE
    )
    extract_result = await ext.run()
    assert any(c.name == "Industry_Required" for c in extract_result.components)

    # Step 3: generate.
    gen = GenerateOrchestrator(process_id="sample", out_dir=out, engram=engram)
    gen_result = await gen.run(extract_result.components)

    # Tier 1 rule for the Industry_Required validation should be present.
    assert (out / "tier1" / "vr_Industry_Required.py").is_file()
    assert (out / "tier1" / "__init__.py").is_file()
    assert gen_result.tier1_count >= 1

    # Step 4: load.
    engine = load_artifact(out / "tier1" / "__init__.py")
    assert len(engine) >= 1
    rules = engine.rules_for("Account", int(OoEStep.CUSTOM_VALIDATION))
    assert any(r.rule_id == "Account.Industry_Required" for r in rules)

    # Step 5a: passing case — Account with Industry → save succeeds.
    runtime = OoERuntime(rules=engine)
    ctx = runtime.execute_save(sobject="Account", record={"Name": "Acme", "Industry": "Tech"})
    assert not ctx.aborted

    # Step 5b: failing case — missing Industry → ValidationFailedError raised.
    with pytest.raises(ValidationFailedError):
        runtime.execute_save(sobject="Account", record={"Name": "Acme"})


@pytest.mark.integration
def test_generate_cli_writes_artifact(tmp_path: Path) -> None:
    out = tmp_path / "cli_artifact"
    rc = subprocess.call(
        [
            "uv",
            "run",
            "offramp",
            "generate",
            "--fixture",
            str(FIXTURE),
            "--out",
            str(out),
            "--process-id",
            f"test_gen_{uuid.uuid4().hex[:6]}",
        ],
    )
    assert rc == 0
    assert (out / "manifest.json").is_file()
    # Tier 1 + Tier 2 + Tier 3 directories all exist (even if some are empty).
    for sub in ("tier1", "tier2", "tier3", "adapters"):
        assert (out / sub).is_dir()
    # We expect at least one Tier 1 rule (Industry_Required) and one
    # Tier 2 workflow (HighValueDiscount approval).
    tier1_files = [
        p for p in (out / "tier1").iterdir() if p.suffix == ".py" and p.name != "__init__.py"
    ]
    tier2_files = list((out / "tier2").iterdir())
    assert tier1_files, "no tier1 rule modules generated"
    assert tier2_files, "no tier2 workflow modules generated"


@pytest.mark.integration
def test_generated_tier2_module_imports_cleanly(tmp_path: Path) -> None:
    """Sanity: every generated Tier 2 file is parseable Python."""
    out = tmp_path / "import_test"
    asyncio.run(_run_generate(out))

    import importlib.util

    tier2 = list((out / "tier2").iterdir())
    assert tier2, "no tier2 modules"
    for f in tier2:
        if f.suffix != ".py":
            continue
        spec = importlib.util.spec_from_file_location(f.stem, f)
        assert spec is not None and spec.loader is not None
        # We don't exec — just byte-compile so unbound deps (temporalio) don't
        # need to be importable. compile() ensures the file is at least valid.
        compile(f.read_text(), str(f), "exec")


async def _run_generate(out_dir: Path) -> None:
    engram = InMemoryEngramClient()
    client = FixturePullClient(FIXTURE)
    ext = ExtractOrchestrator(
        org_alias="sample_org", client=client, engram=engram, fixture_root=FIXTURE
    )
    res = await ext.run()
    gen = GenerateOrchestrator(process_id="t", out_dir=out_dir, engram=engram)
    await gen.run(res.components)
