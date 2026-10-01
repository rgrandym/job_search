"""Application settings, loaded from environment variables and `.env`.

Every tunable (model IDs, scoring weights, thresholds, paths) lives here so that
modules never read `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LLMProviderName = Literal["anthropic", "openai", "openrouter", "codex"]


class ScoringWeights(BaseModel):
    """Relative weight of each scoring metric. Must sum to 1.0."""

    title: float = Field(0.25, ge=0, le=1)
    skills: float = Field(0.35, ge=0, le=1)
    experience: float = Field(0.20, ge=0, le=1)
    location: float = Field(0.10, ge=0, le=1)
    semantic: float = Field(0.10, ge=0, le=1)

    @model_validator(mode="after")
    def _check_sum(self) -> ScoringWeights:
        total = self.title + self.skills + self.experience + self.location + self.semantic
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Scoring weights must sum to 1.0, got {total:.4f}")
        return self


class Settings(BaseSettings):
    """Runtime configuration. Override any field with a `JOBSEARCH_`-prefixed env var."""

    model_config = SettingsConfigDict(
        env_prefix="JOBSEARCH_",
        populate_by_name=True,
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # LLM (defaults; the web UI can change provider/models at runtime, see core/llm)
    llm_provider: LLMProviderName = "anthropic"
    orchestrator_model: str = "claude-opus-5-5"
    worker_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_max_tokens: int = 16000
    llm_refusal_fallback: bool = True
    anthropic_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("JOBSEARCH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("JOBSEARCH_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    openrouter_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("JOBSEARCH_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")
    )

    # Smart matching (LLM screening of the shortlist)
    screen_shortlist_size: int = Field(40, ge=1, le=200)
    screen_batch_size: int = Field(6, ge=1, le=20)
    screen_concurrency: int = Field(4, ge=1, le=16)

    # Matching
    score_threshold: float = Field(70.0, ge=0, le=100)
    retrieval_top_k: int = Field(150, ge=1)
    embedding_dim: int = Field(512, ge=32)
    weights: ScoringWeights = ScoringWeights()

    # Job sources (see src/jobs/sources/)
    reed_api_key: SecretStr | None = None
    cv_library_api_key: SecretStr | None = None
    companies_path: Path = PROJECT_ROOT / "data" / "companies.json"
    inbox_dir: Path = PROJECT_ROOT / "data" / "inbox"
    http_user_agent: str = (
        "ai-job-search/0.1 (personal job search; contact: set JOBSEARCH_HTTP_USER_AGENT)"
    )
    http_timeout_s: float = 20.0
    request_delay_s: float = Field(1.0, ge=0, description="Politeness delay between requests")

    # Paths
    data_dir: Path = PROJECT_ROOT / "data"
    output_dir: Path = PROJECT_ROOT / "output"
    master_cv_path: Path = PROJECT_ROOT / "data" / "master_cv.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
