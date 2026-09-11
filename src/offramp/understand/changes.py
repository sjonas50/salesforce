"""Change log across scans (D.6): what moved in an org between two extracts.

Every :class:`Component` carries a content hash of its canonical form, so two
scans of one org diff by (category, api_name): added, removed, or modified, with
the modification explained at the level a reader cares about — activation
flipped, references gained or lost, code grown or shrunk — not a JSON diff.
The log lives in the knowledge library next to the process definitions
(``changes/<org>.jsonl``) and the last snapshot per org (``snapshots/<org>.json``)
so a scheduled ``offramp sync`` can append to it without keeping every extract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import Component

log = get_logger(__name__)

_REF_KEYS = (
    "objects",
    "fields",
    "fields_written",
    "apex_classes",
    "flows",
    "lwc_bundles",
    "email_alerts",
    "platform_events",
    "message_channels",
    "tabs",
    "flexipages",
)


@dataclass
class ComponentChange:
    category: str
    api_name: str
    change: str  # added | removed | modified
    before_hash: str | None = None
    after_hash: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "api_name": self.api_name,
            "change": self.change,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "details": self.details,
        }

    def describe(self) -> str:
        if self.change != "modified":
            return self.change
        bits = []
        d = self.details
        if "active" in d:
            bits.append(f"active {d['active'][0]} -> {d['active'][1]}")
        for key, delta in d.get("references", {}).items():
            if delta.get("added"):
                bits.append(f"+{key}: {', '.join(delta['added'][:4])}")
            if delta.get("removed"):
                bits.append(f"-{key}: {', '.join(delta['removed'][:4])}")
        if "body_lines" in d:
            bits.append(f"code {d['body_lines'][0]} -> {d['body_lines'][1]} lines")
        return "; ".join(bits) or "content changed"


@dataclass
class ChangeSet:
    org_alias: str
    from_scan: str | None
    to_scan: str
    at: datetime
    added: list[ComponentChange] = field(default_factory=list)
    removed: list[ComponentChange] = field(default_factory=list)
    modified: list[ComponentChange] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "org_alias": self.org_alias,
            "from_scan": self.from_scan,
            "to_scan": self.to_scan,
            "at": self.at.isoformat(),
            "added": [c.to_jsonable() for c in self.added],
            "removed": [c.to_jsonable() for c in self.removed],
            "modified": [c.to_jsonable() for c in self.modified],
        }

    def to_text(self) -> str:
        head = f"{self.at:%Y-%m-%d %H:%M} {self.org_alias}: {self.from_scan or '(first scan)'} -> {self.to_scan}"
        if self.empty:
            return head + "\n  no changes"
        lines = [head]
        for label, items in (
            ("added", self.added),
            ("removed", self.removed),
            ("modified", self.modified),
        ):
            if items:
                lines.append(f"  {label} ({len(items)}):")
                lines += [
                    f"    {c.category:<28} {c.api_name:<40} {c.describe()}" for c in items[:80]
                ]
        return "\n".join(lines)


def _key(c: Component) -> tuple[str, str]:
    return (c.category.value, c.api_name or c.name)


def _refs(c: Component) -> dict[str, set[str]]:
    raw = c.raw if isinstance(c.raw, dict) else {}
    found = raw.get("references")
    refs: dict[str, Any] = found if isinstance(found, dict) else {}
    return {k: {str(x) for x in refs.get(k) or []} for k in _REF_KEYS if refs.get(k)}


def _active(c: Component) -> bool | None:
    raw = c.raw if isinstance(c.raw, dict) else {}
    v = raw.get("active")
    if isinstance(v, bool):
        return v
    status = raw.get("status")
    return status == "Active" if isinstance(status, str) and status else None


def _modification(before: Component, after: Component) -> dict[str, Any]:
    d: dict[str, Any] = {}
    a0, a1 = _active(before), _active(after)
    if a0 is not None and a1 is not None and a0 != a1:
        d["active"] = [a0, a1]
    r0, r1 = _refs(before), _refs(after)
    refs: dict[str, dict[str, list[str]]] = {}
    for key in sorted(set(r0) | set(r1)):
        added = sorted(r1.get(key, set()) - r0.get(key, set()))
        removed = sorted(r0.get(key, set()) - r1.get(key, set()))
        if added or removed:
            refs[key] = {"added": added, "removed": removed}
    if refs:
        d["references"] = refs
    b0 = before.raw.get("body_lines") if isinstance(before.raw, dict) else None
    b1 = after.raw.get("body_lines") if isinstance(after.raw, dict) else None
    if isinstance(b0, int) and isinstance(b1, int) and b0 != b1:
        d["body_lines"] = [b0, b1]
    return d


def diff_components(
    previous: list[Component],
    current: list[Component],
    *,
    org_alias: str,
    from_scan: str | None,
    to_scan: str,
) -> ChangeSet:
    """Added / removed / modified components between two scans of one org."""
    before = {_key(c): c for c in previous}
    after = {_key(c): c for c in current}
    cs = ChangeSet(org_alias=org_alias, from_scan=from_scan, to_scan=to_scan, at=datetime.now(UTC))
    for k in sorted(after.keys() - before.keys()):
        cs.added.append(ComponentChange(k[0], k[1], "added", after_hash=after[k].content_hash))
    for k in sorted(before.keys() - after.keys()):
        cs.removed.append(
            ComponentChange(k[0], k[1], "removed", before_hash=before[k].content_hash)
        )
    for k in sorted(before.keys() & after.keys()):
        b, a = before[k], after[k]
        if b.content_hash != a.content_hash:
            cs.modified.append(
                ComponentChange(
                    k[0],
                    k[1],
                    "modified",
                    before_hash=b.content_hash,
                    after_hash=a.content_hash,
                    details=_modification(b, a),
                )
            )
    return cs


class ChangeLog:
    """Per-org change log + last-snapshot store inside a knowledge library directory."""

    def __init__(self, library: Path) -> None:
        self.library = library
        self.snapshots = library / "snapshots"
        self.changes = library / "changes"

    def _snapshot_path(self, org_alias: str) -> Path:
        return self.snapshots / f"{org_alias}.json"

    def _log_path(self, org_alias: str) -> Path:
        return self.changes / f"{org_alias}.jsonl"

    def last_snapshot(self, org_alias: str) -> tuple[str | None, list[Component]]:
        p = self._snapshot_path(org_alias)
        if not p.is_file():
            return None, []
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("scan_id"), [
            Component.model_validate(c) for c in data.get("components", [])
        ]

    def record(self, org_alias: str, components: list[Component], *, scan_id: str) -> ChangeSet:
        """Diff against the org's last snapshot, append to the log, replace the snapshot."""
        prev_scan, prev = self.last_snapshot(org_alias)
        cs = diff_components(
            prev, components, org_alias=org_alias, from_scan=prev_scan, to_scan=scan_id
        )
        self.changes.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        with self._log_path(org_alias).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cs.to_jsonable()) + "\n")
        self._snapshot_path(org_alias).write_text(
            json.dumps(
                {
                    "scan_id": scan_id,
                    "at": cs.at.isoformat(),
                    "components": [json.loads(c.model_dump_json()) for c in components],
                }
            ),
            encoding="utf-8",
        )
        log.info(
            "changes.recorded",
            org=org_alias,
            scan=scan_id,
            added=len(cs.added),
            removed=len(cs.removed),
            modified=len(cs.modified),
        )
        return cs

    def read(self, org_alias: str, *, last: int | None = None) -> list[dict[str, Any]]:
        p = self._log_path(org_alias)
        if not p.is_file():
            return []
        rows = [
            json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        return rows[-last:] if last else rows

    def history(self, org_alias: str, category: str, api_name: str) -> list[dict[str, Any]]:
        """Every logged change to one component, oldest first."""
        out = []
        for entry in self.read(org_alias):
            for kind in ("added", "removed", "modified"):
                for c in entry.get(kind, []):
                    if c["category"] == category and c["api_name"] == api_name:
                        out.append({"at": entry["at"], "scan": entry["to_scan"], **c})
        return out
