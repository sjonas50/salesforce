"""D.6 change log: component-level diffs between scans, persisted per org."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offramp.core.models import Component
from offramp.understand.changes import ChangeLog, diff_components

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def _components(tmp: Path) -> list[Component]:
    from offramp.cli.__main__ import main

    out = tmp / "x"
    assert main(["extract", "--fixture", str(FIX), "--out", str(out)]) == 0
    d = out if (out / "components.json").is_file() else out / "extract"
    return [Component.model_validate(c) for c in json.loads((d / "components.json").read_text())]


def test_diff_reports_added_removed_and_modified_with_reasons(tmp_path: Path) -> None:
    before = _components(tmp_path)
    after = [c.model_copy(deep=True) for c in before]
    # Drop one, and change another's references + hash.
    removed = next(c for c in after if c.api_name == "UnusedLegacyUtil")
    after.remove(removed)
    flow = next(c for c in after if c.api_name == "LeadRouting")
    flow.raw["references"]["fields"] = [*flow.raw["references"]["fields"], "Lead.Rating"]
    flow.content_hash = "0" * 64
    cs = diff_components(before, after, org_alias="o", from_scan="s1", to_scan="s2")
    assert [c.api_name for c in cs.removed] == ["UnusedLegacyUtil"]
    assert not cs.added
    mod = {c.api_name: c for c in cs.modified}
    assert set(mod) == {"LeadRouting"}
    assert mod["LeadRouting"].details["references"]["fields"]["added"] == ["Lead.Rating"]
    assert "+fields: Lead.Rating" in mod["LeadRouting"].describe()
    assert "modified (1)" in cs.to_text()


def test_changelog_persists_snapshot_and_appends(tmp_path: Path) -> None:
    comps = _components(tmp_path)
    cl = ChangeLog(tmp_path / "lib")
    first = cl.record("o", comps, scan_id="s1")
    assert first.from_scan is None and len(first.added) == len(comps) and not first.modified
    later = [c.model_copy(deep=True) for c in comps]
    trig = next(c for c in later if c.api_name == "LeadDispatcher")
    trig.raw["active"] = False
    trig.content_hash = "1" * 64
    second = cl.record("o", later, scan_id="s2")
    assert second.from_scan == "s1" and [c.api_name for c in second.modified] == ["LeadDispatcher"]
    assert second.modified[0].details.get("active") == [True, False]
    rows = cl.read("o")
    assert [r["to_scan"] for r in rows] == ["s1", "s2"]
    hist = cl.history("o", "apex_trigger", "LeadDispatcher")
    assert [h["change"] for h in hist] == ["added", "modified"]


def test_extract_with_library_records_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from offramp.cli.__main__ import main

    lib = tmp_path / "lib"
    assert (
        main(
            ["extract", "--fixture", str(FIX), "--out", str(tmp_path / "a"), "--library", str(lib)]
        )
        == 0
    )
    assert (
        main(
            ["extract", "--fixture", str(FIX), "--out", str(tmp_path / "b"), "--library", str(lib)]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "(first scan)" in out and "no changes" in out
    capsys.readouterr()
    assert main(["changes", "--library", str(lib), "--org", "sample_org", "--last", "1"]) == 0
    assert "no changes" in capsys.readouterr().out
