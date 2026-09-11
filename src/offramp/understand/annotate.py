"""LLM annotation harness (C23).

Every automation component gets a structured annotation from the LLM; every
detected business process (cluster) gets a narrative. The model never sees a
raw metadata slice: it reads a *dossier* (:mod:`annotate_context`) — the
reverse-engineered process model, the dependency neighbourhood, the static
facts, health findings, live verification results and the source — and must
answer with evidence quotes and an explicit list of unknowns.

Confidence is earned, not declared. The model's own estimate is the starting
point; rules then cap it when the source was hidden or truncated, when it
named unknowns, when it gave no evidence, and when its tier contradicts the
deterministic hint (which also flags the component for review).

The harness is provider-aware via ``LLMSettings.base_url``: an
``api.anthropic.com`` host routes to the Anthropic SDK. Every annotation is
Engram-anchored with the exact prompt + model + output so re-running with a
newer model is a deterministic comparison, not a fresh generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from offramp.core.config import LLMSettings
from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.engram.client import EngramClient
from offramp.understand.annotate_context import (
    Dossier,
    DossierInputs,
    build_dossiers,
    build_process_dossier,
)
from offramp.understand.clustering import BusinessProcess
from offramp.understand.dependencies import DependencyGraph

log = get_logger(__name__)

DomainTag = Literal["sales", "service", "marketing", "compliance", "operations", "other"]
ComplexityBand = Literal["low", "medium", "high"]
RecommendedTier = Literal["tier1_rules", "tier2_temporal", "tier3_langgraph"]

_TIERS = {"tier1_rules", "tier2_temporal", "tier3_langgraph"}
_DOMAINS = {"sales", "service", "marketing", "compliance", "operations", "other"}
_BANDS = {"low", "medium", "high"}


class Annotation(BaseModel):
    """LLM-produced annotation for one component."""

    component_id: str
    summary: str = Field(max_length=300)
    domain: DomainTag
    complexity_band: ComplexityBand
    recommended_tier: RecommendedTier
    confidence: float = Field(ge=0.0, le=1.0)
    model: str
    engram_anchor: str | None = None
    # Earned-confidence trail (all optional so older annotation files still load).
    narrative: str = Field(default="", max_length=1500)
    tier_reasoning: str = Field(default="", max_length=600)
    evidence: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    self_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tier_hint: str = ""
    needs_review: bool = False
    source_available: bool = True
    truncated: bool = False
    deterministic: bool = False
    context_chars: int = 0
    confidence_notes: list[str] = Field(default_factory=list)
    calibrated_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ProcessAnnotation(BaseModel):
    """LLM-produced narrative for one detected business process (cluster)."""

    process_id: str
    name: str = Field(max_length=120)
    narrative: str = Field(max_length=2000)
    domain: DomainTag
    entry_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    tier_mix: dict[str, int] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    model: str
    members: list[str] = Field(default_factory=list)
    engram_anchor: str | None = None


@dataclass
class _RateLimiter:
    """Simple token-bucket-style throttle: ``requests_per_minute`` ceiling."""

    requests_per_minute: int
    _times: list[float]
    _lock: asyncio.Lock

    @classmethod
    def create(cls, rpm: int) -> _RateLimiter:
        return cls(requests_per_minute=rpm, _times=[], _lock=asyncio.Lock())

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < 60.0]
            if len(self._times) >= self.requests_per_minute:
                sleep_for = 60.0 - (now - self._times[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
            self._times.append(now)


class _LLMBackend(Protocol):
    """Provider-agnostic single-shot prompt → JSON response."""

    model: str

    async def complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]: ...


@dataclass
class AnthropicBackend:
    """Anthropic backend (``model`` from settings; default Claude Sonnet 5)."""

    api_key: str
    model: str
    workspace_id: str = ""
    _client: anthropic.AsyncAnthropic | None = None

    def _ensure(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key, default_headers=headers)
        return self._client

    async def complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        client = self._ensure()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        return _extract_json(text)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of free text, tolerating ```json fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"LLM response contained no JSON object: {text[:200]!r}")
        text = text[start : end + 1]
    # strict=False tolerates raw newlines/tabs inside strings, which models emit in long
    # narratives; the second attempt also drops backslash-escaped apostrophes (\'), which
    # JSON forbids.
    try:
        return json.loads(text, strict=False)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        repaired = text.replace("\\'", "'")
        return json.loads(repaired, strict=False)  # type: ignore[no-any-return]


_SYSTEM_PROMPT = """\
You are a Salesforce reverse-engineering analyst on the Off-Ramp platform. You receive a
DOSSIER about one Salesforce component: its identity, FACTS established by static analysis
(do not contradict them), the reverse-engineered PROCESS MODEL, its DEPENDENCY NEIGHBOURHOOD,
and the SOURCE when Salesforce exposes it. Reply with ONLY a JSON object (no markdown).

Schema:
{
  "summary": "<= 300 chars, one sentence: what this component does for the business",
  "narrative": "<= 1500 chars: how it works step by step — triggers, conditions, data read and written, what it calls, what calls it, side effects",
  "domain": "sales" | "service" | "marketing" | "compliance" | "operations" | "other",
  "complexity_band": "low" | "medium" | "high",
  "recommended_tier": "tier1_rules" | "tier2_temporal" | "tier3_langgraph",
  "tier_reasoning": "<= 600 chars: why that tier; if you disagree with the tier hint in FACTS say exactly which fact overrides it",
  "evidence": ["<= 6 short quotes or facts copied from the dossier that support the summary and tier"],
  "unknowns": ["what you could not determine from the dossier and would need to see (empty list if nothing)"],
  "self_confidence": 0.0-1.0
}

Rules:
* Ground every claim in the dossier. Never invent fields, objects, callouts or callers that are
  not in it. If the SOURCE is marked unavailable or truncated, say what that hides in unknowns.
* Tier guidance: tier1_rules = deterministic synchronous validation / computation / assignment;
  tier2_temporal = multi-step, durable state, callouts, scheduled or human waits, async jobs;
  tier3_langgraph = interpretation of unstructured input or judgment calls.
* The "tier hint" line in FACTS is a conclusion, not evidence: quote the facts behind it
  (callouts, async work, steps, source lines), never the hint itself.
* self_confidence reflects how completely the dossier lets you describe the component: 0.9+
  only when source and process model are both visible and consistent.
"""

_PROCESS_PROMPT = """\
You are a Salesforce reverse-engineering analyst on the Off-Ramp platform. You receive a
PROCESS CLUSTER: automation components that the dependency graph groups together, each with
its own annotation, plus the objects they share and the edges between them. Describe the
business process they implement. Reply with ONLY a JSON object (no markdown).

Schema:
{
  "name": "<= 120 chars, a business name for the process (e.g. 'Lead intake and routing')",
  "narrative": "<= 2000 chars: what happens end to end, in execution order where the edges show it; name the components as you go",
  "domain": "sales" | "service" | "marketing" | "compliance" | "operations" | "other",
  "entry_points": ["how the process starts: record saves, schedules, screens, events, API"],
  "risks": ["concrete migration or reliability risks visible in the members (empty if none)"],
  "self_confidence": 0.0-1.0
}
Ground everything in the cluster text; do not invent components or objects.
"""


def _earned_confidence(raw: dict[str, Any], d: Dossier) -> tuple[float, bool, list[str]]:
    """Start from the model's estimate, then apply what we know about what it saw."""
    notes: list[str] = []
    try:
        conf = float(raw.get("self_confidence", raw.get("confidence", 0.5)))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    needs_review = False
    tier = str(raw.get("recommended_tier", ""))
    if tier == d.hint.tier:
        conf = min(1.0, conf + 0.05)
        notes.append("tier agrees with the static hint: +0.05")
    elif tier in _TIERS:
        conf = min(conf, 0.6)
        needs_review = True
        notes.append(
            f"tier {tier} contradicts the static hint {d.hint.tier}: capped at 0.6, review"
        )
    # Hard ceilings for what the model could not have seen or did not show its work on.
    if not d.source_available:
        conf = min(conf, 0.5)
        notes.append("source hidden by Salesforce: capped at 0.5")
    if d.truncated:
        conf = min(conf, 0.75)
        notes.append("source truncated: capped at 0.75")
    unknowns = [str(u) for u in raw.get("unknowns") or [] if str(u).strip()]
    if unknowns:
        conf = min(conf, 0.8)
        notes.append(f"{len(unknowns)} unknown(s) named: capped at 0.8")
    evidence = [str(e) for e in raw.get("evidence") or [] if str(e).strip()]
    if not evidence:
        conf = min(conf, 0.5)
        notes.append("no evidence quoted: capped at 0.5")
    return round(conf, 2), needs_review, notes


@dataclass
class Annotator:
    """Top-level harness: rate-limited, Engram-anchoring, async-safe."""

    backend: _LLMBackend
    engram: EngramClient
    rate_limiter: _RateLimiter
    max_tokens: int = 1024
    component_label: str = "understand.annotate"

    @classmethod
    def from_settings(cls, settings: LLMSettings, *, engram: EngramClient) -> Annotator:
        host = settings.base_url.lower()
        if "anthropic.com" not in host:
            raise NotImplementedError(
                f"Only the Anthropic backend is implemented; got base_url={settings.base_url}. "
                "Add an OpenAI-compatible backend if you need to swap providers."
            )
        backend = AnthropicBackend(
            api_key=settings.api_key.get_secret_value(),
            model=settings.model,
            workspace_id=settings.workspace_id,
        )
        return cls(
            backend=backend,
            engram=engram,
            rate_limiter=_RateLimiter.create(settings.requests_per_minute),
            max_tokens=max(settings.max_tokens, 2048),
        )

    # ---- components ----------------------------------------------------------

    async def annotate_dossier(self, d: Dossier) -> Annotation:
        if d.deterministic is not None:
            det = d.deterministic
            return Annotation(
                component_id=d.component_id,
                summary=str(det["summary"])[:300],
                narrative=str(det.get("narrative", ""))[:1500],
                domain=det.get("domain", "other"),
                complexity_band=det.get("complexity_band", "low"),
                recommended_tier=det.get("recommended_tier", "tier1_rules"),
                tier_reasoning=str(det.get("tier_reasoning", ""))[:600],
                evidence=list(det.get("evidence", [])),
                unknowns=list(det.get("unknowns", [])),
                confidence=float(det.get("confidence", 1.0)),
                self_confidence=None,
                tier_hint=d.hint.tier,
                deterministic=True,
                context_chars=d.chars,
                confidence_notes=["deterministic: no model call"],
                model="rules",
            )
        await self.rate_limiter.wait()
        user = f"Annotate this component.\n\n{d.text}"
        try:
            raw = await self._complete_with_retry(_SYSTEM_PROMPT, user, d.name)
        except (anthropic.APIError, anthropic.APIConnectionError) as exc:
            log.error("understand.annotate.backend_error", error=str(exc), component=d.name)
            raise
        conf, review, notes = _earned_confidence(raw, d)
        tier = str(raw.get("recommended_tier", ""))
        try:
            ann = Annotation(
                component_id=d.component_id,
                summary=str(raw.get("summary", ""))[:300],
                narrative=str(raw.get("narrative", ""))[:1500],
                domain=_domain(raw.get("domain")),
                complexity_band=_band(raw.get("complexity_band")),
                recommended_tier=tier if tier in _TIERS else d.hint.tier,  # type: ignore[arg-type]
                tier_reasoning=str(raw.get("tier_reasoning", ""))[:600],
                evidence=[str(e)[:300] for e in raw.get("evidence") or []][:8],
                unknowns=[str(u)[:300] for u in raw.get("unknowns") or []][:8],
                confidence=conf,
                self_confidence=_float_or_none(raw.get("self_confidence")),
                tier_hint=d.hint.tier,
                needs_review=review,
                source_available=d.source_available,
                truncated=d.truncated,
                context_chars=d.chars,
                confidence_notes=notes,
                model=self.backend.model,
            )
        except Exception as exc:
            log.error(
                "understand.annotate.validation_failed", error=str(exc), raw=raw, component=d.name
            )
            raise
        anchor = await self.engram.anchor(
            self.component_label,
            {
                "component_id": ann.component_id,
                "model": ann.model,
                "system_prompt_hash": _hash_str(_SYSTEM_PROMPT),
                "user_prompt": user,
                "annotation": ann.model_dump(mode="json"),
            },
        )
        ann.engram_anchor = anchor.anchor_id
        return ann

    async def _complete_with_retry(self, system: str, user: str, name: str) -> dict[str, Any]:
        """One retry when the answer is not parseable JSON (the model is told why)."""
        try:
            return await self.backend.complete_json(system, user, self.max_tokens)
        except (ValueError, json.JSONDecodeError) as exc:
            log.info("understand.annotate.retry_json", component=name, error=str(exc)[:120])
            await self.rate_limiter.wait()
            return await self.backend.complete_json(
                system,
                user + "\n\nYour previous reply was not valid JSON (apostrophes must not be "
                "backslash-escaped). Reply again with ONLY the JSON object.",
                self.max_tokens,
            )

    async def annotate_one(self, component: Component) -> Annotation:
        """Annotate a lone component with whatever its own record holds (no graph context)."""
        dossiers = build_dossiers(DossierInputs(components=[component]))
        return await self.annotate_dossier(dossiers[str(component.id)])

    async def annotate_many(
        self,
        components: list[Component],
        *,
        concurrency: int = 4,
        dossiers: dict[str, Dossier] | None = None,
    ) -> list[Annotation]:
        """Annotate a batch with bounded concurrency; a failed component is logged and skipped."""
        if dossiers is None:
            dossiers = build_dossiers(DossierInputs(components=components))
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(c: Component) -> Annotation | None:
            d = dossiers.get(str(c.id))
            if d is None:
                return None
            async with sem:
                try:
                    return await self.annotate_dossier(d)
                except Exception as exc:  # one bad answer must not end the pass
                    log.warning(
                        "understand.annotate.skipped", component=c.name, error=str(exc)[:200]
                    )
                    return None

        results = await asyncio.gather(*(_bounded(c) for c in components))
        done = [a for a in results if a is not None]
        log.info(
            "understand.annotate.done",
            requested=len(components),
            annotated=len(done),
            deterministic=sum(1 for a in done if a.deterministic),
            needs_review=sum(1 for a in done if a.needs_review),
        )
        return done

    # ---- processes -----------------------------------------------------------

    async def annotate_processes(
        self,
        processes: list[BusinessProcess],
        components: list[Component],
        annotations: list[Annotation],
        *,
        graph: DependencyGraph | None = None,
        min_members: int = 2,
        concurrency: int = 4,
    ) -> list[ProcessAnnotation]:
        """A narrative per cluster with at least ``min_members`` annotated automation components."""
        by_id = {str(c.id): c for c in components}
        summaries = {a.component_id: a.model_dump(mode="json") for a in annotations}
        sem = asyncio.Semaphore(concurrency)

        async def _one(p: BusinessProcess) -> ProcessAnnotation | None:
            members = [by_id[i] for i in p.component_ids if i in by_id]
            if sum(1 for m in members if str(m.id) in summaries) < min_members:
                return None  # a narrative needs at least two annotated automation members
            members = [m for m in members if str(m.id) in summaries] + [
                m for m in members if str(m.id) not in summaries
            ][:10]
            text = build_process_dossier(
                p.process_id, p.label, members, summaries, graph, p.object_names
            )
            async with sem:
                await self.rate_limiter.wait()
                try:
                    raw = await self.backend.complete_json(
                        _PROCESS_PROMPT, f"Describe this process.\n\n{text}", self.max_tokens
                    )
                except Exception as exc:
                    log.warning(
                        "understand.annotate.process_skipped",
                        process=p.process_id,
                        error=str(exc)[:200],
                    )
                    return None
            tiers: dict[str, int] = {}
            for m in members:
                t = str(summaries.get(str(m.id), {}).get("recommended_tier", ""))
                if t:
                    tiers[t] = tiers.get(t, 0) + 1
            member_conf = [
                float(summaries[str(m.id)]["confidence"]) for m in members if str(m.id) in summaries
            ]
            self_conf = _float_or_none(raw.get("self_confidence")) or 0.5
            conf = min(self_conf, sum(member_conf) / len(member_conf) if member_conf else 0.5)
            try:
                pa = ProcessAnnotation(
                    process_id=p.process_id,
                    name=str(raw.get("name") or p.label)[:120],
                    narrative=str(raw.get("narrative", ""))[:2000],
                    domain=_domain(raw.get("domain")),
                    entry_points=[str(e)[:200] for e in raw.get("entry_points") or []][:8],
                    risks=[str(r)[:300] for r in raw.get("risks") or []][:8],
                    tier_mix=tiers,
                    confidence=round(conf, 2),
                    model=self.backend.model,
                    members=[m.name for m in members],
                )
            except Exception as exc:
                log.warning(
                    "understand.annotate.process_invalid", process=p.process_id, error=str(exc)
                )
                return None
            anchor = await self.engram.anchor(
                "understand.annotate.process",
                {
                    "process_id": p.process_id,
                    "model": pa.model,
                    "system_prompt_hash": _hash_str(_PROCESS_PROMPT),
                    "user_prompt": text,
                    "annotation": pa.model_dump(mode="json"),
                },
            )
            pa.engram_anchor = anchor.anchor_id
            return pa

        results = await asyncio.gather(*(_one(p) for p in processes))
        done = [r for r in results if r is not None]
        log.info(
            "understand.annotate.processes_done", requested=len(processes), annotated=len(done)
        )
        return done


def _domain(v: Any) -> DomainTag:
    return v if v in _DOMAINS else "other"


def _band(v: Any) -> ComplexityBand:
    return v if v in _BANDS else "medium"


def _float_or_none(v: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


# ---- persistence -----------------------------------------------------------


def save_annotations(
    annotations: list[Annotation], components: list[Component], path: Path
) -> None:
    """Write annotations with each component's content hash so another scan can reuse them."""
    by_id = {str(c.id): c for c in components}
    rows = []
    for a in annotations:
        c = by_id.get(a.component_id)
        rows.append(
            {
                **a.model_dump(mode="json"),
                "content_hash": c.content_hash if c else None,
                "name": c.name if c else None,
                "category": c.category.value if c else None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")


def load_annotations(path: Path, components: list[Component]) -> list[Annotation]:
    """Reuse saved annotations for the components whose content hash still matches."""
    by_hash = {c.content_hash: c for c in components}
    out: list[Annotation] = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        c = by_hash.get(str(row.get("content_hash")))
        if c is None:
            continue
        data = {k: v for k, v in row.items() if k not in {"content_hash", "name", "category"}}
        data["component_id"] = str(c.id)
        out.append(Annotation.model_validate(data))
    return out


def save_process_annotations(annotations: list[ProcessAnnotation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([a.model_dump(mode="json") for a in annotations], indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_process_annotations(path: Path) -> list[ProcessAnnotation]:
    return [
        ProcessAnnotation.model_validate(r) for r in json.loads(path.read_text(encoding="utf-8"))
    ]


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
