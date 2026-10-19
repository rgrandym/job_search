"""Shared fixtures. Tests never call a real LLM or the network."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from src.core.config import Settings
from src.cv.models import MasterCV
from src.services import search_service

T = TypeVar("T", bound=BaseModel)
ENSURE_COMPANY_BOARDS = search_service._ensure_company_boards  # the real hook, for its own tests
ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "data" / "examples"


class FakeLLM:
    """`LLMProvider` that returns canned objects keyed by output model."""

    def __init__(self, responses: dict[type[BaseModel], BaseModel | Callable[[str], Any]]):
        self.responses = responses
        self.calls: list[tuple[str, type[BaseModel]]] = []

    def generate(self, *, system: str, prompt: str, output_model: type[T]) -> T:
        self.calls.append((prompt, output_model))
        resp = self.responses[output_model]
        out = resp(prompt) if callable(resp) else resp
        assert isinstance(out, output_model)
        return out


@pytest.fixture
def master_cv() -> MasterCV:
    return MasterCV.model_validate_json((EXAMPLES / "master_cv.example.json").read_text())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        request_delay_s=0,
        linkedin_delay_s=0,
        linkedin_cooldown_s=0,
        browser_delay_s=0,
        learn_employers=0,  # never in the background during tests
        data_dir=tmp_path,
        output_dir=tmp_path / "output",
        master_cv_path=tmp_path / "master_cv.json",
        inbox_dir=tmp_path / "inbox",
        companies_path=tmp_path / "companies.json",
    )


@pytest.fixture(autouse=True)
def _no_claude_usage_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan windows would run the real `claude -p /usage`; tests stay offline."""
    from src.core.llm import claude_code_backend
    from src.core.llm.types import LLMError

    def unavailable(config_dir: str | None = None) -> str:
        raise LLMError("Claude Code usage is not read in tests")

    monkeypatch.setattr(claude_code_backend, "claude_usage_text", unavailable)


@pytest.fixture(autouse=True)
def _claude_accounts_in_memory() -> None:
    """Each test starts with only the default Claude account, never the user's saved list,
    and no account or plan known to be spent."""
    from src.core.llm import claude_accounts, usage

    usage.configure(None)
    claude_accounts.configure(None)


@pytest.fixture(autouse=True)
def _no_company_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Searches with company sites would discover ~1,000 live websites on first use."""

    async def skip(*_: Any) -> None:
        return None

    monkeypatch.setattr(search_service, "_ensure_company_boards", skip)


@pytest.fixture(autouse=True)
def _no_place_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Searches would ask postcodes.io where each stated town is; `tests/test_places.py`
    tests the lookup with a mocked API."""
    from src.jobs.places import PlaceLookup

    monkeypatch.setattr(PlaceLookup, "find", lambda self, name: None)


@pytest.fixture(autouse=True)
def _title_only_postings_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most fixtures are title-only postings that test other behaviour; the rule that a
    posting without readable requirements is never a match is tested with `CHECKABLE_CHARS`
    restored (`tests/test_screening.py`)."""
    from src.jobs import screener

    monkeypatch.setattr(screener, "CHECKABLE_CHARS", 0)


@pytest.fixture(autouse=True)
def _no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never launch the user's Chrome; browser tests pass their own `run`."""
    from src.jobs.sources import browser

    monkeypatch.setattr(browser, "find_chrome", lambda settings: None)
