"""Runtime configuration. Every value (and every credential) comes from the environment / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ProviderName = Literal["anthropic", "openai", "gemini", "openrouter", "ollama", "vllm"]


def _env(prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(env_prefix=prefix, env_file=".env", extra="ignore", populate_by_name=True)


class ProviderSettings(BaseSettings):
    """Connection details for one LLM provider. `model` must be set explicitly for hosted providers."""

    api_key: SecretStr | None = None
    model: str | None = None
    base_url: str | None = None
    rpm: int = 60  # client-side requests-per-minute ceiling (token bucket)
    context_tokens: int | None = None  # set for servers with small windows; the gateway pre-flights against it


class AnthropicSettings(ProviderSettings):
    model_config = _env("ANTHROPIC_")
    model: str | None = "claude-opus-5-5"
    effort: Literal["low", "medium", "high"] = "low"  # classification-style tasks don't need deep thinking


class OpenAISettings(ProviderSettings):
    model_config = _env("OPENAI_")


class GeminiSettings(ProviderSettings):
    model_config = _env("GEMINI_")


class OpenRouterSettings(ProviderSettings):
    model_config = _env("OPENROUTER_")
    base_url: str | None = "https://openrouter.ai/api/v1"


class OllamaSettings(ProviderSettings):
    model_config = _env("OLLAMA_")
    base_url: str | None = "http://localhost:11434/v1"
    model: str | None = "gemma3:4b"
    rpm: int = 600
    context_tokens: int | None = 2048  # Ollama's default; match it to the server's OLLAMA_CONTEXT_LENGTH


class VLLMSettings(ProviderSettings):
    model_config = _env("VLLM_")
    base_url: str | None = "http://localhost:8000/v1"
    rpm: int = 600


class TypeSafeSettings(ProviderSettings):
    """Native Jev (TypeSafe System-One). Not a chat model: used as a classification strategy."""
    model_config = _env("TYPESAFE_")
    model: str | None = "jev-latest"
    base_url: str | None = "https://api.typesafe.ai/v1"
    rpm: int = 300


class Settings(BaseSettings):
    model_config = _env()

    # --- storage / data -------------------------------------------------------------------------
    database_url: str = "postgresql://medallion:medallion@localhost:55432/medallion"
    raw_data_path: Path = PROJECT_ROOT / "data" / "raw_tickets.csv"
    source_name: str = "facility_tickets"
    seeds_dir: Path = PROJECT_ROOT / "config" / "seeds"
    taxonomy_path: Path = PROJECT_ROOT / "config" / "taxonomy.yaml"

    # --- logging ---------------------------------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- LLM routing: ordered fallback chain. Empty => deterministic (offline) strategies only. ----
    llm_providers: Annotated[list[ProviderName], NoDecode] = Field(default_factory=list)
    llm_timeout_s: float = 60.0
    llm_max_attempts: int = 4
    llm_backoff_base_s: float = 0.5
    llm_backoff_max_s: float = 20.0
    llm_concurrency: int = 4
    llm_batch_size: int = 25
    llm_max_tokens_per_run: int = 400_000  # hard budget guard, provider-agnostic
    llm_max_cost_usd_per_run: float = 2.0  # hard budget guard (USD) where pricing is known
    dedup_safety_bits: float = 3.0  # content dedup margin over log2(N^2); higher = fewer merges, never false ones
    timezone: str = "Asia/Kolkata"  # log timestamps
    llm_auto_approve_confidence: float = 0.85  # threshold on trust (or on agent confidence without a judge)
    # Optional LLM-as-judge ("<provider>[:<model>]"). When set, auto-approval uses human-weighted trust.
    judge: str | None = None
    # Classification backend: "llm" (provider chain) or "jev" (native Jev typed choices via OpenRouter or TypeSafe)
    classifier_backend: Literal["llm", "jev"] = "llm"
    trust_weight_human: float = 0.6
    trust_weight_judge: float = 0.3
    trust_weight_agent: float = 0.1
    circuit_failure_threshold: int = 5
    circuit_reset_s: float = 30.0

    # --- events ----------------------------------------------------------------------------------
    kafka_bootstrap_servers: str | None = None
    kafka_topic: str = "medallion.events"
    outbox_poll_interval_s: float = 2.0

    # --- API / worker ----------------------------------------------------------------------------
    api_key: SecretStr | None = None  # when set, every API call must send X-API-Key
    api_url: str = "http://localhost:8000"  # used by `medallion submit`
    api_rate_limit_rpm: int = 30
    worker_poll_interval_s: float = 2.0
    job_max_attempts: int = 3
    job_lease_s: int = 900

    # --- alert thresholds (observability) --------------------------------------------------------
    alert_quarantine_rate: float = 0.01
    alert_null_rate_delta: float = 0.05
    alert_llm_failure_rate: float = 0.2

    @field_validator("llm_providers", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

    def provider(self, name: ProviderName) -> ProviderSettings:
        return {
            "anthropic": AnthropicSettings,
            "openai": OpenAISettings,
            "gemini": GeminiSettings,
            "openrouter": OpenRouterSettings,
            "ollama": OllamaSettings,
            "vllm": VLLMSettings,
        }[name]()


@lru_cache
def get_settings() -> Settings:
    return Settings()
