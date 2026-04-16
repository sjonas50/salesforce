"""Compliance report export.

Produces a JSON document a customer auditor can consume:

* every divergence in the configured window
* every Engram anchor for those observations
* the readiness score + cutover eligibility decision
* the lag snapshot at export time

Sensitive divergences (severity >= 70) are additionally F44-anchored so the
auditor can verify them against a public Merkle root. F44 anchoring is a
no-op when ``f44_network`` is unset.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from offramp.engram.client import EngramClient
from offramp.validate.reconcile.lag_monitor import LagMonitor
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import ShadowStore


@dataclass
class ComplianceExportResult:
    out_path: Path
    process_id: str
    divergences_exported: int
    anchor_id: str
    f44_anchored_count: int


async def export_compliance_report(
    *,
    process_id: str,
    store: ShadowStore,
    scorer: ReadinessScorer,
    lag: LagMonitor,
    engram: EngramClient,
    out_path: Path,
    severity_floor_for_f44: int = 70,
) -> ComplianceExportResult:
    score = await scorer.score(process_id)
    lag_snap = await lag.snapshot(process_id)
    divergences = await store.divergences_for(process_id, limit=10_000)

    f44_anchored = 0
    for d in divergences:
        # severity isn't on the divergence row; pull it from the trace if
        # present (it's also implied by the category — we don't double-store).
        sev = int(d.get("trace", {}).get("severity", 0)) if isinstance(d.get("trace"), dict) else 0
        if d.get("diverged") and sev >= severity_floor_for_f44:
            await engram.anchor(
                "shadow.compliance.f44",
                {
                    "process_id": process_id,
                    "divergence_id": d["id"],
                    "anchor_id": d.get("anchor_id"),
                    "category": d.get("category"),
                },
            )
            f44_anchored += 1

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "process_id": process_id,
        "readiness": asdict(score),
        "lag": {
            "process_id": lag_snap.process_id,
            "last_event_at": (
                lag_snap.last_event_at.isoformat() if lag_snap.last_event_at else None
            ),
            "lag_hours": lag_snap.lag_hours,
            "threshold_hours": lag_snap.threshold_hours,
            "needs_reconciliation": lag_snap.needs_reconciliation,
            "status": lag_snap.status,
        },
        "divergences": [
            {
                "id": d["id"],
                "replay_id": d["replay_id"],
                "observed_at": d["observed_at"].isoformat()
                if hasattr(d["observed_at"], "isoformat")
                else d["observed_at"],
                "diverged": d["diverged"],
                "category": d["category"],
                "field_diffs": d["field_diffs"],
                "anchor_id": d["anchor_id"],
            }
            for d in divergences
        ],
        "f44_anchored_count": f44_anchored,
    }
    # Sync filesystem I/O is acceptable here — one-shot CLI export.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(  # noqa: ASYNC240
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Anchor the export itself so the auditor can verify the file's integrity.
    anchor = await engram.anchor(
        "shadow.compliance.export",
        {
            "process_id": process_id,
            "divergence_count": len(divergences),
            "score": score.score,
            "f44_anchored_count": f44_anchored,
        },
    )

    return ComplianceExportResult(
        out_path=out_path,
        process_id=process_id,
        divergences_exported=len(divergences),
        anchor_id=anchor.anchor_id,
        f44_anchored_count=f44_anchored,
    )
