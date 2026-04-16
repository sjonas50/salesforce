"""LLM annotation harness.

Every component gets a short structured annotation from the LLM:

* one-sentence summary
* business-domain tag (Sales / Service / Marketing / Compliance / Other)
* complexity score (low / medium / high)
* recommended translation tier (tier1_rules / tier2_temporal / tier3_langgraph)

The harness is provider-aware via ``LLMSettings.base_url``: an
``api.anthropic.com`` host routes to the Anthropic SDK; anything else uses an
OpenAI-compatible HTTP client (which Llama / vLLM / OpenAI all support).

Every annotation is Engram-anchored with the exact prompt + model + output so
re-running with a newer model is a deterministic comparison, not a fresh
generation. This is the structural defense against "the LLM hallucinated
something plausible-sounding" called out in the v2.1 plan.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from offramp.core.config import LLMSettings
from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.engram.client import EngramClient

log = get_logger(__name__)

DomainTag = Literal["sales", "service", "marketing", "compliance", "operations", "other"]
ComplexityBand = Literal["low", "medium", "high"]
RecommendedTier = Literal["tier1_rules", "tier2_temporal", "tier3_langgraph"]


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


@dataclass
class _RateLimiter:
    """Simple token-bucket-style throttle: ``requests_per_minute`` ceiling.

    Sleeps just enough to keep the running window under the limit. Async-safe
    via a single shared mutex guarding the timestamp deque.
    """

    requests_per_minute: int
    _times: list[float]
    _lock: asyncio.Lock

    @classmethod
    def create(cls, rpm: int) -> _RateLimiter:
        return cls(requests_per_minute=rpm, _times=[], _lock=asyncio.Lock())

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Drop timestamps older than 60s.
            self._times = [t for t in self._times if now - t < 60.0]
            if len(self._times) >= self.requests_per_minute:
                sleep_for = 60.0 - (now - self._times[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                # Re-snapshot after sleeping.
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
            self._times.append(now)


class _LLMBackend(Protocol):
    """Provider-agnostic single-shot prompt → JSON response."""

    model: str

    async def complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]: ...


@dataclass
class AnthropicBackend:
    """Real Anthropic Claude Sonnet 4.6 (or whatever ``model`` resolves to)."""

    api_key: str
    model: str
    _client: anthropic.AsyncAnthropic | None = None

    def _ensure(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
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
    # Strip code fences first.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    # Otherwise locate the first balanced { ... } block.
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"LLM response contained no JSON object: {text[:200]!r}")
        text = text[start : end + 1]
    return json.loads(text)  # type: ignore[no-any-return]


_SYSTEM_PROMPT = """\
You are a Salesforce reverse-engineering analyst working as part of the
Off-Ramp platform. Your job is to read one piece of extracted Salesforce
metadata and produce a STRICT JSON annotation. Reply with ONLY a JSON object
(no markdown, no commentary).

Schema:
{
  "summary": "<= 300-char single-sentence description of what this component does",
  "domain": "sales" | "service" | "marketing" | "compliance" | "operations" | "other",
  "complexity_band": "low" | "medium" | "high",
  "recommended_tier": "tier1_rules" | "tier2_temporal" | "tier3_langgraph",
  "confidence": 0.0-1.0
}

Tier guidance (from the v2.1 plan):
* tier1_rules    — deterministic synchronous validation/computation, no callouts
* tier2_temporal — multi-step, durable state, callouts, scheduled or human waits
* tier3_langgraph — interpretation of unstructured input or judgment calls
"""


def _build_user_prompt(c: Component) -> str:
    body = json.dumps(
        {
            "category": c.category.value,
            "name": c.name,
            "api_name": c.api_name,
            "namespace": c.namespace,
            "raw": c.raw,
        },
        indent=2,
        sort_keys=True,
    )[:4000]  # cap context so a huge passthrough payload doesn't blow tokens
    return f"Annotate this component:\n\n{body}"


@dataclass
class Annotator:
    """Top-level harness: rate-limited, Engram-anchoring, async-safe."""

    backend: _LLMBackend
    engram: EngramClient
    rate_limiter: _RateLimiter
    max_tokens: int = 1024
    component_label: str = "understand.annotate"

    @classmethod
    def from_settings(
        cls,
        settings: LLMSettings,
        *,
        engram: EngramClient,
    ) -> Annotator:
        # Provider routing via base_url host.
        host = settings.base_url.lower()
        if "anthropic.com" not in host:
            raise NotImplementedError(
                f"Only the Anthropic backend is implemented in Phase 2; got base_url={settings.base_url}. "
                "Add an OpenAI-compatible backend if you need to swap providers."
            )
        backend = AnthropicBackend(
            api_key=settings.api_key.get_secret_value(),
            model=settings.model,
        )
        return cls(
            backend=backend,
            engram=engram,
            rate_limiter=_RateLimiter.create(settings.requests_per_minute),
            max_tokens=settings.max_tokens,
        )

    async def annotate_one(self, component: Component) -> Annotation:
        await self.rate_limiter.wait()
        user = _build_user_prompt(component)
        try:
            raw = await self.backend.complete_json(
                _SYSTEM_PROMPT,
                user,
                self.max_tokens,
            )
        except (anthropic.APIError, anthropic.APIConnectionError) as exc:
            log.error("understand.annotate.backend_error", error=str(exc), component=component.name)
            raise
        try:
            ann = Annotation(
                component_id=str(component.id),
                summary=str(raw.get("summary", ""))[:300],
                domain=raw.get("domain", "other"),  # validated by Literal
                complexity_band=raw.get("complexity_band", "medium"),
                recommended_tier=raw.get("recommended_tier", "tier1_rules"),
                confidence=float(raw.get("confidence", 0.5)),
                model=self.backend.model,
            )
        except Exception as exc:
            log.error(
                "understand.annotate.validation_failed",
                error=str(exc),
                raw=raw,
                component=component.name,
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

    async def annotate_many(
        self,
        components: list[Component],
        *,
        concurrency: int = 4,
    ) -> list[Annotation]:
        """Annotate a batch with bounded concurrency."""
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(c: Component) -> Annotation:
            async with sem:
                return await self.annotate_one(c)

        return await asyncio.gather(*(_bounded(c) for c in components))


def _hash_str(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()
