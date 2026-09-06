"""Compare two extract outputs of the same automation (C25: source-vs-org diff).

A customer's repository and their org are two views of one thing. Scanning both
and diffing them, component by component, turns "is the reverse engineering
right?" into a list: components in one view only, edges the parser found on one
path but not the other, and process definitions whose canonical form differs.
Anything on that list is either drift between repo and org (useful to the
customer) or an extraction bug (useful to us) — the report says which is likelier.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger

log = get_logger(__name__)

# Node kinds that describe the org's data model rather than its automation; a
# missing schema field on the source side is expected (source trees carry
# custom fields only), so those edges are reported separately, not as gaps.
_SCHEMA_KINDS = {"field", "object", "record_type"}


@dataclass
class ExtractView:
    """One extract directory, indexed for comparison."""

    label: str
    components: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str], set[tuple[str, str, str]]] = field(default_factory=dict)
    processes: dict[str, dict[str, Any]] = field(default_factory=dict)  # (kind.name) -> def

    @classmethod
    def load(cls, extract_dir: Path, label: str | None = None) -> ExtractView:
        v = cls(label=label or extract_dir.as_posix())
        g = json.loads((extract_dir / "graph.json").read_text(encoding="utf-8"))
        by_id = {n["id"]: n for n in g["nodes"]}
        for n in g["nodes"]:
            if n.get("kind") == "component":
                v.components[(str(n.get("category")), str(n["api_name"]))] = n
        for e in g["edges"]:
            if e.get("evidence") == "dependency_api":
                continue  # cross-check rows are not parser evidence
            s, t = by_id.get(e["source_id"]), by_id.get(e["target_id"])
            if not s or not t or s.get("kind") != "component":
                continue
            key = (str(s.get("category")), str(s["api_name"]))
            v.edges.setdefault(key, set()).add(
                (str(t.get("category") or t.get("kind")), str(t["api_name"]), str(e.get("kind")))
            )
        pj = extract_dir / "processes.json"
        if pj.is_file():
            raw = json.loads(pj.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("processes", [])
            for p in items:
                v.processes[f"{p.get('kind')}.{p.get('name')}"] = p
        return v


@dataclass
class ComponentDiff:
    category: str
    api_name: str
    only_in_a: list[tuple[str, str, str]] = field(default_factory=list)
    only_in_b: list[tuple[str, str, str]] = field(default_factory=list)
    schema_only_in_a: int = 0  # edges to schema nodes (expected to differ by path)
    schema_only_in_b: int = 0
    process_differs: bool = False
    process_fidelity: tuple[str | None, str | None] = (None, None)

    @property
    def significant(self) -> bool:
        return bool(self.only_in_a or self.only_in_b or self.process_differs)


@dataclass
class CompareReport:
    a: str
    b: str
    missing_in_b: list[tuple[str, str]] = field(default_factory=list)
    missing_in_a: list[tuple[str, str]] = field(default_factory=list)
    diffs: list[ComponentDiff] = field(default_factory=list)
    shared: int = 0
    agreeing_edges: int = 0

    @property
    def clean(self) -> bool:
        return (
            not self.missing_in_a
            and not self.missing_in_b
            and not any(d.significant for d in self.diffs)
        )

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "a": self.a,
            "b": self.b,
            "shared_components": self.shared,
            "agreeing_edges": self.agreeing_edges,
            "missing_in_b": [{"category": c, "api_name": n} for c, n in self.missing_in_b],
            "missing_in_a": [{"category": c, "api_name": n} for c, n in self.missing_in_a],
            "component_diffs": [
                {
                    "category": d.category,
                    "api_name": d.api_name,
                    "only_in_a": [list(x) for x in d.only_in_a],
                    "only_in_b": [list(x) for x in d.only_in_b],
                    "schema_only_in_a": d.schema_only_in_a,
                    "schema_only_in_b": d.schema_only_in_b,
                    "process_differs": d.process_differs,
                    "process_fidelity": list(d.process_fidelity),
                }
                for d in self.diffs
                if d.significant or d.schema_only_in_a or d.schema_only_in_b
            ],
            "clean": self.clean,
        }

    def to_text(self) -> str:
        lines = [
            f"Compare: A={self.a}  B={self.b}",
            f"  shared components: {self.shared}   agreeing edges: {self.agreeing_edges}",
        ]
        if self.missing_in_b:
            lines.append(f"  only in A ({len(self.missing_in_b)}):")
            lines += [f"    {c:<28} {n}" for c, n in self.missing_in_b[:40]]
        if self.missing_in_a:
            lines.append(f"  only in B ({len(self.missing_in_a)}):")
            lines += [f"    {c:<28} {n}" for c, n in self.missing_in_a[:40]]
        sig = [d for d in self.diffs if d.significant]
        if sig:
            lines.append(f"  components whose edges or definition differ ({len(sig)}):")
            for d in sig[:60]:
                lines.append(f"    {d.category:<28} {d.api_name}")
                for t in d.only_in_a[:8]:
                    lines.append(f"      A only: {t[2]} -> {t[0]} {t[1]}")
                for t in d.only_in_b[:8]:
                    lines.append(f"      B only: {t[2]} -> {t[0]} {t[1]}")
                if d.process_differs:
                    fa, fb = d.process_fidelity
                    lines.append(f"      process definition differs (fidelity A={fa} B={fb})")
        sch = sum(d.schema_only_in_a + d.schema_only_in_b for d in self.diffs)
        if sch:
            lines.append(
                f"  schema-node edges present on one side only: {sch} "
                "(expected when one side is a source tree: standard fields are not in source)"
            )
        lines.append("  RESULT: " + ("clean" if self.clean else "differences found"))
        return "\n".join(lines)


def _process_hash(p: dict[str, Any]) -> str:
    return str(p.get("id") or "")


def compare_views(a: ExtractView, b: ExtractView, *, a_is_subset: bool = False) -> CompareReport:
    """``a_is_subset``: A is a repository holding part of the org (B); components only
    in B are the org's other automation, not differences."""
    rep = CompareReport(a=a.label, b=b.label)
    keys_a, keys_b = set(a.components), set(b.components)
    rep.missing_in_b = sorted(keys_a - keys_b)
    rep.missing_in_a = [] if a_is_subset else sorted(keys_b - keys_a)
    proc_by_name: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for side, view in (("a", a), ("b", b)):
        for pkey, p in view.processes.items():
            proc_by_name[pkey][side] = p
    for key in sorted(keys_a & keys_b):
        rep.shared += 1
        ea, eb = a.edges.get(key, set()), b.edges.get(key, set())
        rep.agreeing_edges += len(ea & eb)
        d = ComponentDiff(category=key[0], api_name=key[1])
        for t in sorted(ea - eb):
            if t[0] in _SCHEMA_KINDS:
                d.schema_only_in_a += 1
            else:
                d.only_in_a.append(t)
        for t in sorted(eb - ea):
            if t[0] in _SCHEMA_KINDS:
                d.schema_only_in_b += 1
            else:
                d.only_in_b.append(t)
        pk = f"{key[0]}.{key[1]}"
        pa, pb = proc_by_name.get(pk, {}).get("a"), proc_by_name.get(pk, {}).get("b")
        if pa and pb:
            d.process_fidelity = (pa.get("fidelity"), pb.get("fidelity"))
            d.process_differs = _process_hash(pa) != _process_hash(pb)
        rep.diffs.append(d)
    log.info(
        "understand.compare.done",
        shared=rep.shared,
        missing_in_a=len(rep.missing_in_a),
        missing_in_b=len(rep.missing_in_b),
        differing=sum(1 for d in rep.diffs if d.significant),
    )
    return rep


def compare_extracts(a_dir: Path, b_dir: Path, *, a_is_subset: bool = False) -> CompareReport:
    return compare_views(
        ExtractView.load(a_dir, a_dir.as_posix()),
        ExtractView.load(b_dir, b_dir.as_posix()),
        a_is_subset=a_is_subset,
    )
