"""``offramp compare``: the source-vs-org diff report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from offramp.understand.compare import ExtractView, compare_extracts, compare_views

FIX = Path(__file__).parents[1] / "integration" / "fixtures" / "sample_org"


def _extract(tmp_path: Path, name: str) -> Path:
    from offramp.cli.__main__ import main

    out = tmp_path / name
    assert main(["extract", "--fixture", str(FIX), "--out", str(out)]) == 0
    return out if (out / "graph.json").is_file() else out / "extract"


def test_identical_extracts_compare_clean(tmp_path: Path) -> None:
    a = _extract(tmp_path, "a")
    b = tmp_path / "b" / "extract"
    shutil.copytree(a, b)
    rep = compare_extracts(a, b)
    assert rep.clean and rep.shared > 40 and rep.agreeing_edges > 100
    assert "RESULT: clean" in rep.to_text()


def test_dropped_edge_and_component_are_reported(tmp_path: Path) -> None:
    a = _extract(tmp_path, "a")
    b = tmp_path / "b" / "extract"
    shutil.copytree(a, b)
    g = json.loads((b / "graph.json").read_text())
    by_id = {n["id"]: n for n in g["nodes"]}
    # B lost the LeadRouting -> LeadScoringService call and the whole LeadValidationHandler class.
    g["edges"] = [
        e
        for e in g["edges"]
        if not (
            by_id[e["source_id"]]["api_name"] == "LeadRouting"
            and by_id[e["target_id"]]["api_name"] == "LeadScoringService"
        )
    ]
    g["nodes"] = [n for n in g["nodes"] if n["api_name"] != "LeadValidationHandler"]
    (b / "graph.json").write_text(json.dumps(g))
    rep = compare_extracts(a, b)
    assert not rep.clean
    assert ("apex_class", "LeadValidationHandler") in rep.missing_in_b
    d = next(x for x in rep.diffs if x.api_name == "LeadRouting")
    assert ("apex_class", "LeadScoringService", "calls") in d.only_in_a and not d.only_in_b
    js = rep.to_jsonable()
    assert js["clean"] is False and js["missing_in_b"][0]["api_name"] == "LeadValidationHandler"
    text = rep.to_text()
    assert "A only: calls -> apex_class LeadScoringService" in text


def test_schema_only_edges_are_not_gaps(tmp_path: Path) -> None:
    a = _extract(tmp_path, "a")
    va = ExtractView.load(a, "a")
    vb = ExtractView.load(a, "b")
    # Drop every field edge from B: a source tree without standard fields looks like this.
    for key, edges in vb.edges.items():
        vb.edges[key] = {t for t in edges if t[0] != "field"}
    rep = compare_views(va, vb)
    assert rep.clean and sum(d.schema_only_in_a for d in rep.diffs) > 0


def test_cli_compare_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offramp.cli.__main__ import main

    a = _extract(tmp_path, "a")
    capsys.readouterr()  # drop the extract command's own output
    assert main(["compare", "--a", str(a), "--b", str(a), "--json"]) == 0
    out = capsys.readouterr().out
    # structlog lines precede the report (and are themselves JSON under LOG_FORMAT=json);
    # the report is the pretty-printed document starting with its "a" key.
    assert json.loads(out[out.index('{\n  "a"') :])["clean"] is True


def test_subset_mode_ignores_components_only_in_the_org(tmp_path: Path) -> None:
    a = _extract(tmp_path, "a")
    va = ExtractView.load(a, "repo")
    vb = ExtractView.load(a, "org")
    vb.components[("apex_class", "SomethingElseInTheOrg")] = {"api_name": "SomethingElseInTheOrg"}
    assert not compare_views(va, vb).clean
    assert compare_views(va, vb, a_is_subset=True).clean
