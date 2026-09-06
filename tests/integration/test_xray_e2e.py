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
from typing import Any

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


pytestmark = [pytest.mark.integration]


def _run_xray(out_dir: Path, *extra: str) -> int:
    return subprocess.call(
        [
            "uv",
            "run",
            "offramp",
            "xray",
            "--fixture",
            str(FIXTURE),
            "--out",
            str(out_dir),
            "--skip-annotations",
            *extra,
        ],
    )


def _check_payload(out_dir: Path) -> dict[str, Any]:
    html = out_dir / "xray.html"
    js = out_dir / "xray.json"
    assert html.is_file() and html.stat().st_size > 0
    assert js.is_file() and js.stat().st_size > 0
    payload: dict[str, Any] = json.loads(js.read_text())
    assert payload["schema_version"] == "2.0"
    assert payload["org_alias"] == FIXTURE.name
    assert len(payload["components"]) > 0
    assert len(payload["business_processes"]) > 0
    assert len(payload["ooe_surface_audit"]) == 21
    stats = payload["graph"]["stats"]
    assert stats["edges"] > 100 and len(stats["by_evidence"]) >= 8
    assert payload["summary"]["unused_custom_fields"] >= 1
    assert payload["summary"]["legacy_automation"] >= 2
    assert any(si["object"] == "Lead" for si in payload["save_impacts"])
    text = html.read_text()
    for section in (
        "Where is this used",
        "Save impact",
        "Unused custom fields",
        "Legacy automation",
    ):
        assert section in text
    return payload


def test_xray_in_memory_runs_end_to_end(tmp_path: Path) -> None:
    """No FalkorDB, no LLM: the whole X-Ray pipeline in memory."""
    out_dir = tmp_path / "xray"
    assert _run_xray(out_dir, "--no-graph-db") == 0
    _check_payload(out_dir)
    assert subprocess.call(["uv", "run", "python", "scripts/verify_xray.py", str(out_dir)]) == 0


@pytest.mark.skipif(not _has_falkordb(), reason="FalkorDB not reachable")
def test_xray_with_falkordb(tmp_path: Path) -> None:
    out_dir = tmp_path / "xray"
    assert _run_xray(out_dir, "--graph-name", f"test_xray_{uuid.uuid4().hex[:8]}") == 0
    _check_payload(out_dir)


@pytest.mark.skipif(not _has_anthropic_key(), reason="ANTHROPIC_API_KEY not set")
@pytest.mark.skipif(not _has_falkordb(), reason="FalkorDB not reachable")
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
