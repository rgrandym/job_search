"""Single-user workspace state: LLM config, Master CV, profile memory, last search.

The app is local and single-user, so this is one process-wide object. Everything that must
survive a restart is a JSON file under `data/` (git-ignored).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from src.core.config import LLMProviderName, Settings, get_settings
from src.core.llm import ChatModel, LLMConfig, Role, make_chat, make_structured
from src.core.llm_provider import LLMProvider
from src.cv import master_cv_manager as mgr
from src.cv.models import MasterCV, TailoredCV
from src.jobs.models import JobPosting, MatchReport, SearchQuery
from src.jobs.profile_memory import ProfileMemory


class Workspace:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        data = self.settings.data_dir
        self._saved_keys: dict[str, str] = {}
        self._codex_ok, self._codex_checked = False, -1e9
        self.llm_config_path = data / "llm_config.json"
        self.llm = self._load_llm_config()
        self.memory = ProfileMemory(data / "profile_summaries.json")
        self.master_cv: MasterCV | None = (
            mgr.load(self.settings.master_cv_path)
            if self.settings.master_cv_path.exists()
            else None
        )
        self.active_cv_id: str | None = "master" if self.master_cv is not None else None
        self.last_query: SearchQuery | None = None
        self.last_report: MatchReport | None = None
        self.tailored: dict[str, TailoredCV] = {}

    # ---------------------------------------------------------------- LLM

    def _load_llm_config(self) -> LLMConfig:
        base = LLMConfig.from_settings(self.settings)
        if not self.llm_config_path.exists():
            return base
        saved: dict[str, Any] = json.loads(self.llm_config_path.read_text())
        keys: dict[str, str] = saved.pop("api_keys", {})
        provider: LLMProviderName = saved.get("provider", base.provider)
        cfg = LLMConfig.from_settings(self.settings, provider).model_copy(update=saved)
        if keys.get(provider):
            cfg.api_key = SecretStr(keys[provider])
        self._saved_keys = keys
        return cfg

    def set_llm_config(self, update: dict[str, Any], api_key: str | None = None) -> LLMConfig:
        """Change provider/models (and optionally store an API key for that provider)."""
        keys = self._saved_keys
        provider: LLMProviderName = update.get("provider", self.llm.provider)
        if api_key:
            keys[provider] = api_key
        cfg = LLMConfig.from_settings(self.settings, provider)
        merged = {**self.llm.model_dump(exclude={"api_key", "provider"}), **update}
        cfg = cfg.model_copy(update={k: v for k, v in merged.items() if k != "api_key"})
        if keys.get(provider):
            cfg.api_key = SecretStr(keys[provider])
        if provider == "codex":
            self._codex_checked = -1e9
        self.llm, self._saved_keys = cfg, keys
        payload = cfg.model_dump(mode="json", exclude={"api_key"}) | {"api_keys": keys}
        self.llm_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.llm_config_path.write_text(json.dumps(payload, indent=2))
        return cfg

    def saved_key(self, provider: str) -> str | None:
        """API key saved from the UI for `provider` (env keys are not returned)."""
        return self._saved_keys.get(provider)

    def llm_ready(self) -> bool:
        """Models chosen and credentials available (Anthropic may use env/CLI credentials)."""
        if not (self.llm.orchestrator_model and self.llm.worker_model):
            return False
        if self.llm.api_key is not None:
            return True
        if self.llm.provider == "anthropic":
            from src.core.llm.anthropic_backend import has_ambient_credentials

            return has_ambient_credentials()
        if self.llm.provider == "codex":
            return self._codex_logged_in()
        return False

    def _codex_logged_in(self) -> bool:
        """`codex login status`, cached for 30s (it spawns a process)."""
        import time

        from src.core.llm.codex_backend import codex_status

        now = time.monotonic()
        if now - self._codex_checked > 30:
            self._codex_ok = bool(codex_status()["logged_in"])
            self._codex_checked = now
        return self._codex_ok

    def structured(self, role: Role = "worker") -> LLMProvider:
        return make_structured(self.llm, role)

    def chat(self, role: Role) -> ChatModel:
        return make_chat(self.llm, role)

    # ---------------------------------------------------------------- CV

    def save_master_cv(self, cv: MasterCV) -> None:
        mgr.save(cv, self.settings.master_cv_path)
        self.master_cv = cv
        self.active_cv_id = "master"

    # ---------------------------------------------------------------- results

    def job(self, job_id: str) -> JobPosting | None:
        if self.last_report is None:
            return None
        return next((r.job for r in self.last_report.all_results() if r.job.id == job_id), None)

    @property
    def output_dir(self) -> Path:
        return self.settings.output_dir


@lru_cache(maxsize=1)
def get_workspace() -> Workspace:
    return Workspace()
