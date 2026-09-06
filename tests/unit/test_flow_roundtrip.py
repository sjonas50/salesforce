"""Round-trip: ProcessDefinition -> Flow XML -> extract -> the same ProcessDefinition."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from offramp.core.process import Fidelity, ProcessDefinition
from offramp.knowledge.flow_xml import RenderError, to_flow_xml

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def _definitions(fixture: Path, tmp: Path) -> dict[str, ProcessDefinition]:
    from offramp.cli.verify import load_processes

    out = tmp / "out"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "offramp.cli",
            "extract",
            "--fixture",
            str(fixture),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    d = out if (out / "processes.json").is_file() else out / "extract"
    return {p.name: p for p in load_processes(d, [])}


def _strip_labels(x: Any) -> Any:
    """Labels are cosmetic and Salesforce requires them on deploy; identity ignores them."""
    if isinstance(x, dict):
        return {k: _strip_labels(v) for k, v in x.items() if k != "label"}
    if isinstance(x, list):
        return [_strip_labels(v) for v in x]
    return x


def _comparable(p: ProcessDefinition) -> Any:
    return _strip_labels(
        p.model_dump(
            mode="json", exclude={"id", "fingerprint", "sources", "summary", "tags", "description"}
        )
    )


def test_full_fidelity_fixture_flows_survive_the_round_trip(tmp_path: Path) -> None:
    originals = _definitions(FIX, tmp_path / "a")
    full = {
        n: p
        for n, p in originals.items()
        if p.fidelity is Fidelity.FULL and p.kind.endswith("flow")
    }
    assert len(full) >= 3, sorted(full)
    # A minimal fixture tree holding only the rendered flows (plus the objects they touch).
    rt = tmp_path / "rt"
    (rt / "flows").mkdir(parents=True)
    shutil.copytree(FIX / "objects", rt / "objects")
    for name, p in full.items():
        (rt / "flows" / f"{name}.flow-meta.xml").write_text(to_flow_xml(p))
    reparsed = _definitions(rt, tmp_path / "b")
    for name, p in full.items():
        assert name in reparsed, name
        assert _comparable(reparsed[name]) == _comparable(p), name


def test_unsupported_kinds_raise_render_error() -> None:
    from offramp.core.process import Step, StepKind, Trigger, TriggerKind

    wf = ProcessDefinition(
        name="X", kind="workflow_rule", trigger=Trigger(kind=TriggerKind.RECORD_SAVE)
    )
    with pytest.raises(RenderError):
        to_flow_xml(wf)
    orch = ProcessDefinition(
        name="O",
        kind="autolaunched_flow",
        trigger=Trigger(kind=TriggerKind.INVOCATION),
        steps=[Step(id="S", kind=StepKind.CALL_PROCESS, extras={"stage_steps": ["a"]})],
    )
    with pytest.raises(RenderError):
        to_flow_xml(orch)


def test_flow_deploy_zip_holds_package_and_flow() -> None:
    import io
    import zipfile

    from offramp.verify.runner import flow_deploy_zip

    z = zipfile.ZipFile(io.BytesIO(flow_deploy_zip("LeadRouting_rt", "<Flow/>")))
    assert set(z.namelist()) == {"package.xml", "flows/LeadRouting_rt.flow"}
    assert "<members>LeadRouting_rt</members>" in z.read("package.xml").decode()
