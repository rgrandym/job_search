"""Single-user workspace state: LLM config, Master CV, profile memory, last search.

The app is local and single-user, so this is one process-wide object. Everything that must
survive a restart is a JSON file under `data/` (git-ignored).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr, TypeAdapter

from src.core.config import LLMProviderName, Settings, get_settings
from src.core.llm import ChatModel, LLMConfig, Role, UsageSink, make_chat, make_structured
from src.core.llm_provider import LLMProvider
from src.cv import master_cv_manager as mgr
from src.cv.models import JDAnalysis, MasterCV, TailoredCV
from src.jobs.models import JobPosting, MatchReport, MatchResult, SavedJob, SearchQuery
from src.jobs.profile_memory import ProfileMemory


class Workspace:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        data = self.settings.data_dir
        self._saved_keys: dict[str, str] = {}
        self._cli_login: dict[str, tuple[bool, float]] = {}  # provider -> (ok, checked_at)
        self.llm_config_path = data / "llm_config.json"
        self.llm = self._load_llm_config()
        self.memory = ProfileMemory(
            data / "profile_summaries.json", self.settings.output_dir / "profiles"
        )
        self.master_cv: MasterCV | None = (
            mgr.load(self.settings.master_cv_path)
            if self.settings.master_cv_path.exists()
            else None
        )
        self.active_cv_id: str | None = "master" if self.master_cv is not None else None
        self.last_query: SearchQuery | None = None
        self.last_report: MatchReport | None = None
        self.last_models: str | None = None  # who judged last_report (`history.HistoryItem`)
        self.tailored: dict[str, TailoredCV] = {}
        # JD analyses by hash of the job text: the CV and the cover letter share one call.
        self.jd_analyses: dict[str, JDAnalysis] = {}

    # ---------------------------------------------------------------- LLM

    def _load_llm_config(self) -> LLMConfig:
        base = LLMConfig.from_settings(self.settings)
        if not self.llm_config_path.exists():
            return base
        saved: dict[str, Any] = legacy_llm_keys(json.loads(self.llm_config_path.read_text()))
        keys: dict[str, str] = saved.pop("api_keys", {})
        provider: LLMProviderName = saved.get("provider", base.provider)
        cfg = LLMConfig.from_settings(self.settings, provider).model_copy(update=saved)
        if keys.get(provider):
            cfg.api_key = SecretStr(keys[provider])
        self._saved_keys = keys
        return cfg

    def set_llm_config(
        self, update: dict[str, Any], api_key: str | None = None, profile_api_key: str | None = None
    ) -> LLMConfig:
        """Change provider/models (and optionally store an API key for that provider, and for
        the profile's own provider)."""
        keys = self._saved_keys
        provider: LLMProviderName = update.get("provider", self.llm.provider)
        if api_key:
            keys[provider] = api_key
        if profile_api_key and update.get("profile_provider"):
            keys[update["profile_provider"]] = profile_api_key
        cfg = LLMConfig.from_settings(self.settings, provider)
        merged = {**self.llm.model_dump(exclude={"api_key", "provider"}), **update}
        cfg = cfg.model_copy(update={k: v for k, v in merged.items() if k != "api_key"})
        if keys.get(provider):
            cfg.api_key = SecretStr(keys[provider])
        self._cli_login.pop(provider, None)
        self.llm, self._saved_keys = cfg, keys
        payload = cfg.model_dump(mode="json", exclude={"api_key"}) | {"api_keys": keys}
        self.llm_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.llm_config_path.write_text(json.dumps(payload, indent=2))
        return cfg

    def saved_key(self, provider: str) -> str | None:
        """API key saved from the UI for `provider` (env keys are not returned)."""
        return self._saved_keys.get(provider)

    def provider_config(self, provider: LLMProviderName) -> LLMConfig:
        """`self.llm` on another provider, with that provider's saved or env credential."""
        if provider == self.llm.provider:
            return self.llm
        key = self.saved_key(provider)
        env_key = LLMConfig.from_settings(self.settings, provider).api_key
        return self.llm.model_copy(
            update={"provider": provider, "api_key": SecretStr(key) if key else env_key}
        )

    def llm_ready(self) -> bool:
        """Models chosen and credentials available, for the profile's provider too."""
        if not (self.llm.quality_model and self.llm.screening_model):
            return False
        return self.provider_ready(self.llm.provider) and self.provider_ready(
            self.llm.profile_provider_for()
        )

    def provider_ready(self, provider: LLMProviderName) -> bool:
        """Credentials available for `provider` (Anthropic may use env/CLI credentials)."""
        if self.provider_config(provider).api_key is not None:
            return True
        if provider == "anthropic":
            from src.core.llm.anthropic_backend import has_ambient_credentials

            return has_ambient_credentials()
        if provider in ("codex", "claude_code"):
            return self._cli_logged_in(provider)
        return False

    def _cli_logged_in(self, provider: str) -> bool:
        """CLI sign-in status, cached for 30s (it spawns a process)."""
        import time

        from src.core.llm.claude_code_backend import claude_code_status
        from src.core.llm.codex_backend import codex_status

        now = time.monotonic()
        ok, checked = self._cli_login.get(provider, (False, -1e9))
        if now - checked > 30:
            status = codex_status() if provider == "codex" else claude_code_status()
            ok = bool(status["logged_in"])
            self._cli_login[provider] = (ok, now)
        return ok

    def structured(
        self,
        role: Role = "screening",
        usage_sink: UsageSink | None = None,
        purpose: str = "structured output",
    ) -> LLMProvider:
        cfg = self.llm
        if role == "profile":
            cfg = self.provider_config(cfg.profile_provider_for())
        return make_structured(cfg, role, usage_sink, purpose)

    def chat(self, role: Role) -> ChatModel:
        return make_chat(self.llm, role)

    # ---------------------------------------------------------------- CV

    def save_master_cv(self, cv: MasterCV) -> None:
        mgr.save(cv, self.settings.master_cv_path)
        self.master_cv = cv
        self.active_cv_id = "master"

    # ---------------------------------------------------------------- results

    def job(self, job_id: str) -> JobPosting | None:
        """A job in current results, saved jobs, history, or generated documents."""
        result = self.result(job_id)
        return result.job if result is not None else None

    def result(self, job_id: str) -> MatchResult | None:
        """Find a job in current results, saved jobs, history, or generated documents."""
        if self.last_report is not None:
            hit = next((r for r in self.last_report.all_results() if r.job.id == job_id), None)
            if hit is not None:
                return hit
        saved = next((s.result for s in self.saved_jobs() if s.result.job.id == job_id), None)
        if saved is not None:
            return saved
        from src.services import history, tracker

        historical = history.find_result(self, job_id)
        if historical is not None:
            return historical
        job = tracker.document_job(self, job_id)
        if job is not None:
            return MatchResult(job=job)
        return next(
            (entry.application_result for entry in tracker.load(self).values()
             if entry.application_result is not None and entry.application_result.job.id == job_id),
            None,
        )

    @property
    def saved_path(self) -> Path:
        return self.settings.data_dir / "saved_jobs.json"

    def saved_jobs(self) -> list[SavedJob]:
        """Saved jobs, newest first (empty when none or unreadable)."""
        try:
            return TypeAdapter(list[SavedJob]).validate_json(self.saved_path.read_bytes())
        except (OSError, ValueError):
            return []

    @property
    def output_dir(self) -> Path:
        return self.settings.output_dir


def legacy_llm_keys(saved: dict[str, Any]) -> dict[str, Any]:
    """Map a config saved before the model roles were renamed (orchestrator -> quality,
    worker -> screening, one shared effort -> one per role)."""
    out = dict(saved)
    if "orchestrator_model" in out:
        out.setdefault("quality_model", out.pop("orchestrator_model"))
    if "worker_model" in out:
        out.setdefault("screening_model", out.pop("worker_model"))
    if "effort" in out:
        effort = out.pop("effort")
        out.setdefault("quality_effort", effort)
        out.setdefault("screening_effort", effort)
    return out


@lru_cache(maxsize=1)
def get_workspace() -> Workspace:
    return Workspace()
