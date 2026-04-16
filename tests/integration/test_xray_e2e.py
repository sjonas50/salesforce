"""Phase 2 end-to-end test against the fixture org.

Exercises the full pipeline with REAL services:
  - real FalkorDB (must be reachable on FALKORDB_URL)
  - real Anthropic Claude Sonnet 4.6 (must have ANTHROPIC_API_KEY)

The test caps the annotated subset to 6 components to keep the API bill
small while still proving the harness works end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_org"


def _has_falkordb() -> bool:
    try:
        from falkordb import FalkorDB

        url = os.environ.get("FALKORDB_URL", "redis://localhost:6379")
        host = url.replace("redis://", "").split(":")[0]
        port = int(url.replace("redis://", "").split(":")[1]) if ":" in url else 6379
        FalkorDB(host=host, port=port).list_graphs()
        return True
    except Exception:
        return False


def _has_anthropic_key() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY"):
        return True
    # Fall back to .env at the repo root — pydantic-settings reads it at runtime
    # but pytest's collection process doesn't. Same logic as production load.
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return False
    for line in env_path.read_text().splitlines():
        if line.startswith(("ANTHROPIC_API_KEY=", "LLM_API_KEY=")):
            _, _, val = line.partition("=")
            if val.strip().split("#")[0].strip():
                return True
    return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_falkordb(), reason="FalkorDB not reachable"),
]


def test_xray_skip_annotations_runs_end_to_end(tmp_path: Path) -> None:
    """Cheap path: full xray pipeline without LLM calls.

    Validates everything except the annotation subsystem; runs in <2s and
    proves the FalkorDB integration + clustering + report rendering shape.
    """
    out_dir = tmp_path / "xray"
    rc = subprocess.call(
        [
            "uv",
            "run",
            "offramp",
            "xray",
            "--fixture",
            str(FIXTURE),
            "--out",
            str(out_dir),
            "--graph-name",
            f"test_xray_{uuid.uuid4().hex[:8]}",
            "--skip-annotations",
        ],
    )
    assert rc == 0, f"offramp xray exited {rc}"
    html = out_dir / "xray.html"
    js = out_dir / "xray.json"
    assert html.is_file() and html.stat().st_size > 0
    assert js.is_file() and js.stat().st_size > 0

    payload = json.loads(js.read_text())
    assert payload["schema_version"] == "1.0"
    assert payload["org_alias"] == FIXTURE.name
    assert len(payload["components"]) > 0
    # At least one cluster detected.
    assert len(payload["business_processes"]) > 0
    # OoE audit included with all 21 steps.
    assert len(payload["ooe_surface_audit"]) == 21


@pytest.mark.skipif(not _has_anthropic_key(), reason="ANTHROPIC_API_KEY not set")
def test_xray_with_real_annotations(tmp_path: Path) -> None:
    """Hit the real Sonnet 4.6 API on a small subset to prove the harness works.

    This test makes ACTUAL Anthropic API calls. It runs with concurrency=2
    against a fixture org of ~25 components to stay cheap. Fails open
    (skipped) when the API key is absent.
    """
    out_dir = tmp_path / "xray_with_llm"
    rc = subprocess.call(
        [
            "uv",
            "run",
            "offramp",
            "xray",
            "--fixture",
            str(FIXTURE),
            "--out",
            str(out_dir),
            "--graph-name",
            f"test_xray_llm_{uuid.uuid4().hex[:8]}",
            "--annotation-concurrency",
            "2",
        ],
    )
    assert rc == 0, f"offramp xray exited {rc}"
    payload = json.loads((out_dir / "xray.json").read_text())
    annotated = [c for c in payload["components"] if c["annotation"] is not None]
    assert len(annotated) == len(payload["components"]), (
        "every component should have an annotation when LLM enabled"
    )
    # Spot-check shape of one annotation.
    sample = annotated[0]["annotation"]
    assert sample["model"]
    assert sample["domain"] in {
        "sales",
        "service",
        "marketing",
        "compliance",
        "operations",
        "other",
    }
    assert sample["recommended_tier"] in {"tier1_rules", "tier2_temporal", "tier3_langgraph"}
    assert sample["engram_anchor"]
