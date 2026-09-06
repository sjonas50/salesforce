"""File-backed knowledge store: a content-addressed process library with scan history.

Layout under ``library/``::

    index.json                 process id → summary row (name, kind, objects, fingerprint, sources)
    processes/<id>.json        full ProcessDefinition
    code/<sha256>.apex         source bodies for code-derived definitions
    scans/<scan_id>.json       what one scan contributed (org, time, process ids, graph stats)
    graphs/<scan_id>.json      the dependency graph of that scan

No services are required. :mod:`offramp.knowledge.falkor` mirrors the same
content into a persistent FalkorDB graph for interactive exploration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from offramp.core.logging import get_logger
from offramp.core.process import ProcessDefinition, ProcessSource

log = get_logger(__name__)


class IndexRow(BaseModel):
    id: str
    name: str
    label: str = ""
    kind: str
    fingerprint: str
    objects: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)
    fidelity: str
    active: bool = True
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    first_seen: datetime
    last_seen: datetime
    sources: list[ProcessSource] = Field(default_factory=list)

    @property
    def orgs(self) -> list[str]:
        return sorted({s.org_alias for s in self.sources})


class ScanRecord(BaseModel):
    scan_id: str
    org_alias: str
    scanned_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    component_count: int = 0
    process_ids: list[str] = Field(default_factory=list)
    new_process_ids: list[str] = Field(default_factory=list)
    graph_stats: dict[str, Any] = Field(default_factory=dict)
    source: str = ""


@dataclass
class Family:
    """Processes that share a shape (same step kinds and operators)."""

    fingerprint: str
    members: list[IndexRow] = field(default_factory=list)


class KnowledgeStore:
    """Persistent process library on disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes_dir = root / "processes"
        self.code_dir = root / "code"
        self.scans_dir = root / "scans"
        self.graphs_dir = root / "graphs"
        self.index_path = root / "index.json"
        self._index: dict[str, IndexRow] | None = None

    # ---- index -------------------------------------------------------------------

    def index(self) -> dict[str, IndexRow]:
        if self._index is None:
            self._index = {}
            if self.index_path.is_file():
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                self._index = {k: IndexRow.model_validate(v) for k, v in data.items()}
        return self._index

    def _save_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        rows = {k: v.model_dump(mode="json") for k, v in sorted(self.index().items())}
        self.index_path.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")

    # ---- ingest ------------------------------------------------------------------

    def ingest(
        self,
        processes: list[ProcessDefinition],
        *,
        org_alias: str,
        scan_id: str | None = None,
        graph: dict[str, Any] | None = None,
        component_count: int = 0,
        source: str = "",
    ) -> ScanRecord:
        """Add one scan's processes. Existing ids gain a source; new ids are written."""
        for d in (self.processes_dir, self.code_dir, self.scans_dir, self.graphs_dir):
            d.mkdir(parents=True, exist_ok=True)
        scan_id = scan_id or _scan_id(org_alias)
        idx = self.index()
        now = datetime.now(UTC)
        rec = ScanRecord(
            scan_id=scan_id, org_alias=org_alias, component_count=component_count, source=source
        )
        for p in processes:
            if not p.id:
                p.finalize()
            for s in p.sources:
                s.scan_id = s.scan_id or scan_id
            rec.process_ids.append(p.id)
            row = idx.get(p.id)
            if row is None:
                if p.code:
                    code_hash = hashlib.sha256(p.code.encode()).hexdigest()
                    (self.code_dir / f"{code_hash}.apex").write_text(p.code, encoding="utf-8")
                stored = p.model_copy(update={"code": None})
                (self.processes_dir / f"{p.id}.json").write_text(
                    stored.model_dump_json(indent=1), encoding="utf-8"
                )
                idx[p.id] = IndexRow(
                    id=p.id,
                    name=p.name,
                    label=p.label,
                    kind=p.kind,
                    fingerprint=p.fingerprint,
                    objects=p.objects,
                    fields=p.touched_fields(),
                    calls=p.calls,
                    fidelity=p.fidelity.value,
                    active=p.active,
                    tags=p.tags,
                    summary=p.summary,
                    first_seen=now,
                    last_seen=now,
                    sources=list(p.sources),
                )
                rec.new_process_ids.append(p.id)
            else:
                row.last_seen = now
                known = {(s.org_alias, s.api_name, s.scan_id) for s in row.sources}
                for s in p.sources:
                    if (s.org_alias, s.api_name, s.scan_id) not in known:
                        row.sources.append(s)
                if p.summary and not row.summary:
                    row.summary = p.summary
                self._append_source(p.id, p.sources)
        if graph is not None:
            (self.graphs_dir / f"{scan_id}.json").write_text(
                json.dumps(graph, sort_keys=True), encoding="utf-8"
            )
            rec.graph_stats = dict(graph.get("stats", {}))
        (self.scans_dir / f"{scan_id}.json").write_text(
            rec.model_dump_json(indent=1), encoding="utf-8"
        )
        self._save_index()
        log.info(
            "knowledge.ingest",
            scan=scan_id,
            org=org_alias,
            processes=len(rec.process_ids),
            new=len(rec.new_process_ids),
        )
        return rec

    def _append_source(self, pid: str, sources: list[ProcessSource]) -> None:
        p = self.get(pid)
        if p is None:
            return
        known = {(s.org_alias, s.api_name, s.scan_id) for s in p.sources}
        changed = False
        for s in sources:
            if (s.org_alias, s.api_name, s.scan_id) not in known:
                p.sources.append(s)
                changed = True
        if changed:
            (self.processes_dir / f"{pid}.json").write_text(
                p.model_copy(update={"code": None}).model_dump_json(indent=1), encoding="utf-8"
            )

    # ---- read --------------------------------------------------------------------

    def get(self, process_id: str) -> ProcessDefinition | None:
        path = self.processes_dir / f"{process_id}.json"
        if not path.is_file():
            return None
        return ProcessDefinition.model_validate_json(path.read_text(encoding="utf-8"))

    def resolve(self, key: str) -> ProcessDefinition | None:
        """Look up by full id, id prefix, or (unique) name / label, case-insensitive."""
        idx = self.index()
        if key in idx:
            return self.get(key)
        by_prefix = [k for k in idx if k.startswith(key)]
        if len(by_prefix) == 1:
            return self.get(by_prefix[0])
        low = key.lower()
        by_name = [k for k, r in idx.items() if r.name.lower() == low or r.label.lower() == low]
        if len(by_name) == 1:
            return self.get(by_name[0])
        by_suffix = [
            k
            for k, r in idx.items()
            if r.name.lower().endswith("." + low) or r.name.lower().rsplit("/", 1)[-1] == low
        ]
        if len(by_suffix) == 1:
            return self.get(by_suffix[0])
        return None

    def list_processes(
        self, *, kind: str | None = None, obj: str | None = None, org: str | None = None
    ) -> list[IndexRow]:
        rows = list(self.index().values())
        if kind:
            rows = [r for r in rows if r.kind == kind]
        if obj:
            rows = [r for r in rows if obj.lower() in {o.lower() for o in r.objects}]
        if org:
            rows = [r for r in rows if org in r.orgs]
        return sorted(rows, key=lambda r: (r.kind, r.name.lower()))

    def search(self, query: str, *, limit: int = 25) -> list[tuple[int, IndexRow]]:
        """Token match over name, label, objects, fields, calls, tags, summary; ranked by hits."""
        tokens = [t for t in re.split(r"[^a-z0-9_]+", query.lower()) if t]
        if not tokens:
            return []
        scored: list[tuple[int, IndexRow]] = []
        for r in self.index().values():
            hay_strong = " ".join([r.name, r.label, *r.objects, *r.tags]).lower()
            hay_weak = " ".join([*r.fields, *r.calls, r.summary or "", r.kind]).lower()
            score = sum(3 for t in tokens if t in hay_strong) + sum(
                1 for t in tokens if t in hay_weak
            )
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
        return scored[:limit]

    def families(self, *, min_size: int = 2) -> list[Family]:
        by_fp: dict[str, Family] = {}
        for r in self.index().values():
            by_fp.setdefault(r.fingerprint, Family(fingerprint=r.fingerprint)).members.append(r)
        fams = [f for f in by_fp.values() if len(f.members) >= min_size]
        fams.sort(key=lambda f: -len(f.members))
        return fams

    def scans(self) -> list[ScanRecord]:
        out = (
            [
                ScanRecord.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self.scans_dir.glob("*.json"))
            ]
            if self.scans_dir.is_dir()
            else []
        )
        return sorted(out, key=lambda s: s.scanned_at)

    def scan(self, scan_id: str) -> ScanRecord | None:
        p = self.scans_dir / f"{scan_id}.json"
        return (
            ScanRecord.model_validate_json(p.read_text(encoding="utf-8")) if p.is_file() else None
        )

    def code(self, p: ProcessDefinition) -> str | None:
        """Source body for a code-derived definition, if stored."""
        if p.code:
            return p.code
        candidates = list(self.code_dir.glob("*.apex")) if self.code_dir.is_dir() else []
        # bodies are keyed by content hash; the process keeps none, so match by name in the body
        for c in candidates:
            text = c.read_text(encoding="utf-8")
            if re.search(
                rf"\b(class|trigger|interface)\s+{re.escape(p.name.split('.')[-1])}\b", text
            ):
                return text
        return None

    def diff(self, scan_a: str, scan_b: str) -> dict[str, list[IndexRow]]:
        """Processes added / removed between two scans (by content id)."""
        a = self.scan(scan_a)
        b = self.scan(scan_b)
        if a is None or b is None:
            raise KeyError(scan_a if a is None else scan_b)
        idx = self.index()
        sa, sb = set(a.process_ids), set(b.process_ids)
        return {
            "added": [idx[i] for i in sorted(sb - sa) if i in idx],
            "removed": [idx[i] for i in sorted(sa - sb) if i in idx],
            "unchanged": [idx[i] for i in sorted(sa & sb) if i in idx],
        }

    def export(self) -> dict[str, Any]:
        """The whole library as one JSON document."""
        return {
            "schema_version": "1.0",
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "processes": [
                proc.model_dump(mode="json")
                for pid in sorted(self.index())
                if (proc := self.get(pid)) is not None
            ],
            "scans": [s.model_dump(mode="json") for s in self.scans()],
        }


def _scan_id(org_alias: str) -> str:
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', org_alias)}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
