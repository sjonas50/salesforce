"""Centralized settings loaded from environment variables.

Production deployments source these via Key Vault / Secrets Manager
init-containers. Local dev uses ``.env`` (loaded by ``pydantic-settings``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SalesforceSettings(BaseSettings):
    """Per-org Salesforce credentials and API config."""

    model_config = SettingsConfigDict(env_prefix="SF_", env_file=".env", extra="ignore")

    org_alias: str = "dev_scratch"
    login_url: str = "https://login.salesforce.com"
    client_id: SecretStr = SecretStr("")
    username: str = "integration@example.com"
    jwt_key_path: Path = Field(default=Path("/secrets/sf_jwt.pem"))
    api_version: str = "66.0"  # pinned per AD-26
    pubsub_host: str = "api.pubsub.salesforce.com:7443"
    cdc_replay_reconcile_threshold_hours: int = 60  # AD-21


class InfraSettings(BaseSettings):
    """Infra connections (Postgres, FalkorDB, event bus, Temporal)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_dsn: str = "postgresql://offramp:offramp@localhost:5432/offramp"
    postgres_shadow_dsn: str = "postgresql://offramp:offramp@localhost:5432/offramp_shadow"
    falkordb_url: str = "redis://localhost:6379"
    event_bus_backend: Literal["redis_streams", "azure_event_hubs", "nats"] = "redis_streams"
    redis_streams_url: str = "redis://localhost:6379"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "offramp-dev"


class LLMSettings(BaseSettings):
    """LLM endpoint config for annotation + Tier 3 agents.

    Defaults to Anthropic Claude Sonnet 4.6. To use a self-hosted Llama or
    another OpenAI-compatible endpoint, override ``base_url`` + ``model``;
    the annotation harness routes by ``base_url`` host.
    """

    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    base_url: str = "https://api.anthropic.com"
    api_key: SecretStr = SecretStr("")
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    requests_per_minute: int = 50


class ProvenanceSettings(BaseSettings):
    """Engram + F44 settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    engram_api_url: str = "http://localhost:8088"
    f44_network: Literal["base-sepolia", "base-mainnet"] = "base-sepolia"


class ObservabilitySettings(BaseSettings):
    """Logging + Datadog."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    datadog_api_key: SecretStr = SecretStr("")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"


class Settings(BaseSettings):
    """Aggregate settings root.

    Each subsection is a separate ``BaseSettings`` so it can be loaded in
    isolation by components that don't need the whole tree (e.g. extract-only
    workers don't need LLM credentials).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    salesforce: SalesforceSettings = Field(default_factory=SalesforceSettings)
    infra: InfraSettings = Field(default_factory=InfraSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    provenance: ProvenanceSettings = Field(default_factory=ProvenanceSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


_cached: Settings | None = None


def get_settings() -> Settings:
    """Process-wide cached Settings instance."""
    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached
