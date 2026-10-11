"""Plan usage, error classification and handing a role to the other plan (`core.llm.usage`,
`core.llm.fallback`). Recorded provider messages; no model is called."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from src.core.config import Settings
from src.core.llm import LLMError, fallback, usage
from src.core.llm.fallback import Candidate, FallbackProvider, clamp_effort, equivalent
from src.services.workspace import Workspace

CLAUDE = [
    Candidate(id="claude-opus-5-5", efforts=["low", "medium", "high", "xhigh", "max"]),
    Candidate(id="claude-opus-5", efforts=["low", "medium", "high", "xhigh", "max"]),
    Candidate(id="claude-sonnet-5-5", efforts=["low", "medium", "high", "xhigh", "max"]),
    Candidate(id="claude-fable-5-1", efforts=["low", "medium", "high", "xhigh", "max"]),
    Candidate(id="claude-haiku-4-5", efforts=[]),
]
CODEX = [
    Candidate(id="gpt-6.1-sol", description="Latest workhorse model."),
    Candidate(id="gpt-6-astra", description="Frontier intelligence for the most demanding work."),
    Candidate(id="gpt-6-sol", description="Previous generation workhorse model."),
    Candidate(id="gpt-6-luna", description="Fast and affordable.", efforts=["low", "medium"]),
    Candidate(id="gpt-5.6-terra", description="Older balanced model."),
]


@pytest.fixture(autouse=True)
def _fresh_usage() -> None:
    usage.configure(None)


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Claude Code error: You've hit your limit · resets 3pm (Europe/London)", "limit"),
        ("You've hit your usage limit. Upgrade to Pro or try again at 10:21 PM.", "limit"),
        ('OpenAI error 429: {"error": {"code": "insufficient_quota"}}', "limit"),
        ("Your credit balance is too low to access the Anthropic API.", "limit"),
        ("Invalid API key · Please run /login", "auth"),
        ("prompt is too long: 210000 tokens > 200000 maximum", "context"),
        ("connect ECONNREFUSED 127.0.0.1:443", "network"),
        ("Claude Code output did not match ScreeningBatch", "other"),
    ],
)
def test_classify_recorded_messages(message: str, kind: str) -> None:
    assert usage.classify(message) == kind
    assert LLMError(message).kind == kind


def test_reset_time_is_read_from_the_message() -> None:
    now = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))
    at = usage.reset_from_text("try again at 10:21 PM", now)
    assert at is not None and time.localtime(at).tm_hour == 22
    tomorrow = usage.reset_from_text("resets 9am", now)
    assert tomorrow is not None and tomorrow > now + 12 * 3600  # 9am has passed: tomorrow's
    assert usage.reset_from_text("limit 5 reached", now) is None  # a bare number is no clock
    assert LLMError("hit your limit |1999999999").resets_at == 1999999999


# ------------------------------------------------------------------ plan windows


def test_codex_windows_are_normalised() -> None:
    view = {
        "plan_type": "plus",
        "primary": {"used_percent": 40, "window_minutes": 300, "resets_at": 100},
        "secondary": {"used_percent": 7, "window_minutes": 10080, "resets_at": 900},
    }
    found = usage.codex_windows(view)
    assert [(w.name, w.used_percent) for w in found.windows] == [("5h", 40.0), ("7d", 7.0)]
    assert found.note == "ChatGPT plus plan"
    assert usage.over_threshold(found, 40) == found.windows[0]
    assert usage.over_threshold(found, 90) is None


def test_claude_windows_persist_across_restarts(tmp_path: Path) -> None:
    usage.configure(tmp_path)
    usage.record_claude_limit(
        {"rateLimitType": "seven_day", "status": "allowed", "resetsAt": 1999999999}
    )
    usage.configure(tmp_path)  # a new process reads what the last one saw
    assert usage.claude_windows().windows[0].name == "7d"


def test_spend_ledger_adds_up_and_persists(tmp_path: Path) -> None:
    usage.configure(tmp_path)
    usage.record_spend("anthropic", "claude-opus-5", 100, 10)
    usage.record_spend("anthropic", "claude-opus-5", 50, 5)
    usage.record_spend("codex", "gpt-6-sol", 10, 1, estimated=True)
    usage.configure(tmp_path)
    rows = {(r.provider, r.model): r for r in usage.spend()}
    opus = rows["anthropic", "claude-opus-5"]
    assert (opus.calls, opus.input_tokens, opus.output_tokens) == (2, 150, 15)
    assert rows["codex", "gpt-6-sol"].estimated


def test_exhaustion_expires_at_the_reset() -> None:
    usage.mark_exhausted("claude_code", time.time() + 60)
    assert usage.exhausted_until("claude_code")
    assert usage.exhausted_until("claude_code", now=time.time() + 61) is None
    until = usage.mark_exhausted("codex", None)  # no stated reset: try again later
    assert until > time.time() + usage.DEFAULT_WAIT_S - 5


# ------------------------------------------------------------------ equivalent models


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5", "gpt-6-sol"),  # previous Opus -> previous sol
        ("claude-opus-5-5", "gpt-6.1-sol"),  # newest -> newest
        ("claude-fable-5-1", "gpt-6-astra"),
        ("claude-sonnet-5-5", "gpt-5.6-terra"),
        ("claude-haiku-4-5", "gpt-6-luna"),
    ],
)
def test_equivalent_codex_model(model: str, expected: str) -> None:
    assert equivalent(model, CLAUDE, CODEX) == expected


def test_equivalent_back_to_claude() -> None:
    assert equivalent("gpt-6-sol", CODEX, CLAUDE) == "claude-opus-5"
    assert equivalent("gpt-6-astra", CODEX, CLAUDE) == "claude-fable-5-1"


def test_nearest_tier_when_the_other_plan_lacks_one() -> None:
    assert equivalent("claude-sonnet-5-5", CLAUDE, CODEX[:3]) in {"gpt-6.1-sol", "gpt-6-sol"}


def test_effort_is_kept_or_clamped() -> None:
    assert clamp_effort("medium", CODEX[0]) == "medium"  # unknown efforts: as it is
    assert clamp_effort("medium", CLAUDE[0]) == "medium"
    assert clamp_effort("high", CODEX[3]) == "medium"  # luna stops at medium here
    assert clamp_effort("medium", CLAUDE[4]) is None  # Haiku takes no effort


# ------------------------------------------------------------------ the fallback provider


class _Out(BaseModel):
    who: str


class _Fake:
    def __init__(self, who: str, error: LLMError | None = None) -> None:
        self.who, self.error, self.calls = who, error, 0

    def generate(self, *, system: str, prompt: str, output_model: type[Any]) -> Any:
        self.calls += 1
        if self.error is not None:
            usage.mark_exhausted("claude_code", self.error.resets_at)  # as `_Reported` does
            raise self.error
        return output_model(who=self.who)


def _pair(error: LLMError | None, mode: fallback.OnLimit = "switch") -> tuple[Any, _Fake, _Fake]:
    primary, alt = _Fake("claude", error), _Fake("codex")
    return FallbackProvider(primary, lambda: alt, "claude_code", "x", mode), primary, alt


def test_a_spent_plan_hands_over_and_stays_handed_over() -> None:
    provider, primary, alt = _pair(LLMError("You've hit your limit", resets_at=time.time() + 600))
    assert provider.generate(system="", prompt="", output_model=_Out).who == "codex"
    assert provider.generate(system="", prompt="", output_model=_Out).who == "codex"
    assert primary.calls == 1  # the spent plan is not asked again before its reset
    assert alt.calls == 2


def test_ask_mode_lets_the_limit_reach_the_caller() -> None:
    provider, _, alt = _pair(LLMError("You've hit your limit"), mode="ask")
    with pytest.raises(LLMError) as caught:
        provider.generate(system="", prompt="", output_model=_Out)
    assert caught.value.kind == "limit" and alt.calls == 0
    with fallback.switching("switch"):  # the user chose "continue on the other model"
        assert provider.generate(system="", prompt="", output_model=_Out).who == "codex"


def test_other_errors_never_switch() -> None:
    provider, _, alt = _pair(LLMError("output did not match"))
    with pytest.raises(LLMError):
        provider.generate(system="", prompt="", output_model=_Out)
    assert alt.calls == 0


# ------------------------------------------------------------------ the workspace's choice


def test_workspace_hands_a_plan_role_to_the_signed_in_other_plan(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(settings)
    ws.llm = ws.llm.model_copy(
        update={"provider": "claude_code", "quality_model": "claude-opus-5", "api_key": None}
    )
    monkeypatch.setattr(fallback, "candidates", lambda p: CLAUDE if p == "claude_code" else CODEX)
    monkeypatch.setattr(ws, "provider_ready", lambda provider: True)

    cfg = ws.fallback_config("quality")

    assert cfg is not None
    assert (cfg.provider, cfg.model_for("quality"), cfg.effort_for("quality")) == (
        "codex",
        "gpt-6-sol",
        "medium",
    )
    monkeypatch.setattr(ws, "provider_ready", lambda provider: provider == "claude_code")
    assert ws.fallback_config("quality") is None  # Codex not signed in: nowhere to go


def test_api_providers_never_hand_over(settings: Settings) -> None:
    ws = Workspace(settings)
    ws.llm = ws.llm.model_copy(update={"provider": "anthropic"})
    assert ws.fallback_config("screening") is None


def test_writing_role_with_its_own_model_keeps_its_effort() -> None:
    from src.core.llm import LLMConfig

    cfg = LLMConfig(
        provider="anthropic",
        quality_model="q",
        screening_model="s",
        cv_model="claude-opus-5",
        cv_provider="claude_code",
        cv_effort="high",
    )
    moved = cfg.with_role_model("letter", "codex", "gpt-6-sol", "high")
    assert (moved.provider_for("letter"), moved.model_for("letter")) == ("codex", "gpt-6-sol")
    assert moved.effort_for("letter") == "high"
