"""scripts/live_status.py: the one-line status the dev pane polls, and `GET /api/activity`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.core import progress
from src.core.llm import ModelUsage, _stamped, usage

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "live_status", ROOT / "scripts" / "live_status.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _load()


def test_status_reports_tasks_gate_and_plans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers: dict[str, Any] = {
        "/api/activity": {
            "searches": 1,
            "tasks": [
                {
                    "title": "Writing tailored CV",
                    "step": "Plan",
                    "done": 1,
                    "total": 4,
                    "calls": 2,
                    "input_tokens": 9000,
                    "output_tokens": 800,
                }
            ],
        },
        "/api/connections": {
            "connections": [
                {
                    "id": "codex",
                    "label": "Codex",
                    "billing": "plan",
                    "windows": {
                        "windows": [
                            {"name": "5h", "used_percent": 40, "resets_at": 1, "tokens": 5400}
                        ]
                    },
                    "exhausted_until": None,
                    "spend_today": [
                        {"calls": 2, "input_tokens": 6000, "output_tokens": 100},
                        {"calls": 1, "input_tokens": 2000, "output_tokens": 0},
                    ],
                },
                {"label": "Claude API", "billing": "key"},
            ]
        },
    }
    monkeypatch.setattr(
        live, "_get", lambda url: next((v for k, v in answers.items() if url.endswith(k)), None)
    )
    monkeypatch.setattr(live, "_up", lambda url: True)
    monkeypatch.setattr(live, "_git_dirty", lambda: 3)
    (tmp_path / ".run").mkdir()
    (tmp_path / ".run" / "gate.json").write_text(json.dumps({"ok": False, "kind": "edit"}))
    monkeypatch.setattr(live, "ROOT", tmp_path)

    out = live.status(plans=True)

    assert out["backend"] and out["searches"] == 1 and out["git_dirty"] == 3
    assert out["tasks"][0]["title"] == "Writing tailored CV"
    assert (out["tasks"][0]["input_tokens"], out["tasks"][0]["output_tokens"]) == (9000, 800)
    assert out["gate"]["ok"] is False
    assert out["plans"] == [
        {
            "id": "codex",
            "label": "Codex",
            "windows": [{"name": "5h", "used_percent": 40, "resets_at": 1, "tokens": 5400}],
            "exhausted_until": None,
            "tokens_today": 8100,
            "calls_today": 3,
        }
    ]


def test_status_when_nothing_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "_get", lambda url: None)
    monkeypatch.setattr(live, "_up", lambda url: False)
    out = live.status(plans=True)
    assert out["backend"] is False and out["tasks"] == [] and "plans" not in out


def test_main_always_prints_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(live, "status", broken)
    assert live.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] is False


def test_active_lists_only_running_tasks() -> None:
    with progress.track("t-run", "Writing a cover letter", total=3):
        progress.step("Reading your CV")
        running = [t.id for t in progress.active()]
    assert "t-run" in running
    assert "t-run" not in [t.id for t in progress.active()]


def test_a_task_counts_the_tokens_of_its_model_calls() -> None:
    usage.configure(None)
    sink = _stamped(None, "codex", None)  # what every structured call reports through
    with progress.track("t-tok", "Search", total=2):
        sink(ModelUsage(model="m", input_tokens=1000, output_tokens=50))
        sink(ModelUsage(model="m", input_tokens=400, output_tokens=10, estimated=True))
        task = next(t for t in progress.active() if t.id == "t-tok")
    assert (task.calls, task.input_tokens, task.output_tokens) == (2, 1400, 60)
    assert task.tokens_estimated
    sink(ModelUsage(model="m", input_tokens=5, output_tokens=5))  # no task open: no-op
    assert progress.snapshot("t-tok") is not None
    assert progress.snapshot("t-tok").calls == 2  # type: ignore[union-attr]


def test_claude_limits_posts_the_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"recorded": 1}'

    def fake_open(req: Any, timeout: float) -> _Resp:
        sent.update(url=req.full_url, body=json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    out = live.claude_limits('[{"kind": "five_hour", "percent_used": 23.5}]')
    assert out == {"ok": True, "recorded": 1}
    assert sent["url"].endswith("/api/usage/claude")
    assert sent["body"] == [{"kind": "five_hour", "percent_used": 23.5}]
    assert live.claude_limits("not json")["ok"] is False
