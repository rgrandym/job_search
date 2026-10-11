"""scripts/live_status.py: the one-line status the dev pane polls, and `GET /api/activity`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.core import progress

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
            "tasks": [{"title": "Writing tailored CV", "step": "Plan", "done": 1, "total": 4}],
        },
        "/api/connections": {
            "connections": [
                {
                    "label": "Codex",
                    "billing": "plan",
                    "windows": {"windows": [{"name": "5h", "used_percent": 40, "resets_at": 1}]},
                    "exhausted_until": None,
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
    assert out["gate"]["ok"] is False
    assert out["plans"] == [
        {
            "label": "Codex",
            "windows": [{"name": "5h", "used_percent": 40, "resets_at": 1}],
            "exhausted_until": None,
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
