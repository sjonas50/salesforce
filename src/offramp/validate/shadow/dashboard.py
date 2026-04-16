"""Render the divergence dashboard (HTML) from the shadow store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from offramp.validate.reconcile.lag_monitor import LagMonitor
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import ShadowStore

_TEMPLATES = Path(__file__).resolve().parents[3].parent / "templates"


_CAT_CLASSES = {
    "gap_event_full_refetch_required": "cat-ge",
    "translation_error": "cat-te",
    "ooe_ordering_mismatch": "cat-oo",
    "governor_limit_behavior": "cat-go",
    "non_deterministic_trigger_ordering": "cat-nd",
    "formula_edge_case": "cat-fe",
    "test_environment_artifact": "cat-ta",
}


async def render_dashboard(
    *,
    process_id: str,
    store: ShadowStore,
    scorer: ReadinessScorer,
    lag: LagMonitor,
    out_path: Path,
) -> None:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("shadow_dashboard.html.j2")
    score = await scorer.score(process_id)
    lag_snap = await lag.snapshot(process_id)
    divergences = await store.divergences_for(process_id, limit=200)

    rendered_divs = [
        {
            **d,
            "field_diffs_pretty": json.dumps(d["field_diffs"], indent=2, sort_keys=True),
            "cat_class": _CAT_CLASSES.get(d.get("category", ""), ""),
        }
        for d in divergences
    ]

    score_class = "ok" if score.score >= 98 else ("warn" if score.score >= 90 else "bad")
    diverged_class = (
        "ok"
        if score.diverged_events == 0
        else ("warn" if score.diverged_events < score.total_events * 0.05 else "bad")
    )
    lag_status = (
        f"{lag_snap.lag_hours:.1f}h since last event (threshold {lag_snap.threshold_hours}h)"
        if lag_snap.lag_hours is not None
        else "no events yet"
    )

    # Render is a one-shot CLI / report operation — sync filesystem I/O OK.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(  # noqa: ASYNC240
        template.render(
            process_id=process_id,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            window_days=score.window_days,
            score=score.score,
            score_class=score_class,
            total=score.total_events,
            diverged=score.diverged_events,
            diverged_class=diverged_class,
            eligible=score.cutover_eligible,
            reason=score.reason,
            lag_status=lag_status,
            divergences=rendered_divs,
        ),
        encoding="utf-8",
    )
