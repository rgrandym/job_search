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
        data_dir=tmp_path,
        output_dir=tmp_path / "output",
        master_cv_path=tmp_path / "master_cv.json",
        inbox_dir=tmp_path / "inbox",
        companies_path=tmp_path / "companies.json",
    )


@pytest.fixture(autouse=True)
def _no_company_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Searches with company sites would discover ~1,000 live websites on first use."""

    async def skip(*_: Any) -> None:
        return None

    monkeypatch.setattr(search_service, "_ensure_company_boards", skip)
