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

LLMProviderName = Literal["anthropic", "openai", "openrouter", "codex", "claude_code"]


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
        # `KEY=` in .env means "not configured", not an empty credential sent to the API.
        env_ignore_empty=True,
        extra="ignore",
    )

    # LLM (defaults; the web UI can change provider/models at runtime, see core/llm)
    llm_provider: LLMProviderName = "anthropic"
    # quality: profile, CV and letters, second opinions, assistant · screening: job matching.
    # The pre-rename variable names (ORCHESTRATOR_MODEL, WORKER_MODEL, LLM_EFFORT) still work.
    quality_model: str = Field(
        "claude-opus-5-5",
        validation_alias=AliasChoices("JOBSEARCH_QUALITY_MODEL", "JOBSEARCH_ORCHESTRATOR_MODEL"),
    )
    screening_model: str = Field(
        "claude-opus-5-5",
        validation_alias=AliasChoices("JOBSEARCH_SCREENING_MODEL", "JOBSEARCH_WORKER_MODEL"),
    )
    quality_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        "medium", validation_alias=AliasChoices("JOBSEARCH_QUALITY_EFFORT", "JOBSEARCH_LLM_EFFORT")
    )
    screening_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        "medium",
        validation_alias=AliasChoices("JOBSEARCH_SCREENING_EFFORT", "JOBSEARCH_LLM_EFFORT"),
    )
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

    # Smart matching (LLM screening of the shortlist). No fixed shortlist size: every posting
    # whose title shares a role word with the target roles is screened, plus `screen_extra`
    # others; `screen_max_jobs` only guards against a runaway search.
    screen_extra: int = Field(10, ge=0, le=100, description="Best other postings also screened")
    screen_max_jobs: int = Field(250, ge=1, le=1000, description="Safety ceiling per search")
    screen_untargeted: int = Field(40, ge=1, le=1000, description="Screened with no target roles")
    screen_batch_size: int = Field(3, ge=1, le=20)
    screen_concurrency: int = Field(6, ge=1, le=16)
    screen_batch_timeout_s: float = Field(240, ge=30, description="Kill and retry a batch after")

    # Matching
    score_threshold: float = Field(60.0, ge=0, le=100)  # v2 listing floor: 60 = stretch
    retrieval_top_k: int | None = Field(None, ge=1, description="None: every eligible posting")
    embedding_dim: int = Field(512, ge=32)
    weights: ScoringWeights = ScoringWeights()

    # Job sources (see src/jobs/sources/)
    reed_api_key: SecretStr | None = None
    cv_library_api_key: SecretStr | None = None
    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_account: str | None = None
    gmail_redirect_uri: str = "http://localhost:8000/api/gmail/callback"
    gmail_frontend_url: str = "http://localhost:5173/"
    companies_path: Path = PROJECT_ROOT / "data" / "companies.json"
    company_workers: int = Field(8, ge=1, le=32, description="Company boards fetched in parallel")
    company_max_details: int = Field(
        30, ge=1, description="Per company, postings opened one by one (Workday, iCIMS, ...)"
    )
    inbox_dir: Path = PROJECT_ROOT / "data" / "inbox"
    http_user_agent: str = (
        "ai-job-search/0.1 (personal job search; contact: set JOBSEARCH_HTTP_USER_AGENT)"
    )
    http_timeout_s: float = 20.0
    request_delay_s: float = Field(1.0, ge=0, description="Politeness delay between requests")
    # LinkedIn's public (logged-out) job search: slow and capped, so a search stays a handful
    # of requests a minute. Full postings are opened only for the shortlist.
    linkedin_delay_s: float = Field(2.5, ge=0, description="Seconds between LinkedIn requests")
    linkedin_max_pages: int = Field(3, ge=1, le=10, description="Result pages per search term")
    linkedin_max_details: int = Field(
        60, ge=0, description="Shortlisted LinkedIn postings opened for the full text per search"
    )
    # LinkedIn's public pages answer browsers; a script identity gets an empty "auth wall".
    browser_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    )

    # Paths
    data_dir: Path = PROJECT_ROOT / "data"
    output_dir: Path = PROJECT_ROOT / "output"
    master_cv_path: Path = PROJECT_ROOT / "data" / "master_cv.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
