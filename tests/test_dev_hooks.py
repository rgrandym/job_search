"""The Claude Code hooks in .claude/hooks: the protected-path guard and the quality gate."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / ".claude" / "hooks" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load("guard_paths")
gate = _load("gate")


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "data/master_cv.json",
        "data/cvs/mine.docx",
        str(ROOT / "data" / "llm_config.json"),
        "output/cvs/x.docx",
        "web/dist/index.html",
        ".agent/skills/job_search/job_schema.json",
    ],
)
def test_guard_denies_protected_paths(path: str) -> None:
    assert guard.reason_for(path)


@pytest.mark.parametrize(
    "path",
    ["data/examples/jobs.example.json", "src/core/config.py", "/tmp/elsewhere.txt", "README.md"],
)
def test_guard_allows_other_paths(path: str) -> None:
    assert guard.reason_for(path) is None


def test_guard_reason_tells_the_model_what_to_do() -> None:
    assert "export-schema" in guard.reason_for(".agent/skills/cv_writer/master_cv_schema.json")


def test_guard_main_prints_a_deny_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "data/job_tracker.json"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_lets_a_broken_payload_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


def test_gate_picks_checks_by_path() -> None:
    assert [c[0] for c in gate.checks_for("src/core/config.py")] == ["ruff format", "ruff", "mypy"]
    assert [c[0] for c in gate.checks_for("tests/test_x.py")] == ["ruff format", "ruff"]
    assert [c[0] for c in gate.checks_for("web/src/App.tsx")] == ["tsc"]
    assert gate.checks_for("README.md") == []


def test_gate_reports_failures_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "STATE", tmp_path / "gate.json")
    monkeypatch.setattr(gate, "run", lambda argv, cwd=ROOT: (False, "E501 line too long"))
    assert gate.execute("edit", [("ruff", ["ruff"], ROOT)]) == 2
    assert "E501" in capsys.readouterr().err
    assert json.loads((tmp_path / "gate.json").read_text())["ok"] is False


def test_gate_stop_runs_once_per_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate, "STATE", tmp_path / "gate.json")
    monkeypatch.setattr(gate, "STOP_STATE", tmp_path / "stop.json")
    monkeypatch.setattr(gate, "diff_key", lambda: "abc")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], cwd: Path = ROOT) -> tuple[bool, str]:
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(gate, "run", fake_run)
    assert gate.on_stop({}) == 0
    assert gate.on_stop({}) == 0  # same diff: not run again
    assert len(calls) == 1
    assert gate.on_stop({"stop_hook_active": True}) == 0
    assert len(calls) == 1


def test_gate_stop_skips_without_python_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "diff_key", lambda: None)
    monkeypatch.setattr(gate, "run", lambda *a, **k: pytest.fail("should not run"))
    assert gate.on_stop({}) == 0


def test_validate_cv_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    assert gate.validate_cv() == 0  # no structured CV: nothing to protect
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "master_cv.json").write_text("{}")
    for code, expected in ((0, 0), (1, 1), (3, 2)):
        done = subprocess.CompletedProcess([], code, stdout="basics: Field required", stderr="")
        monkeypatch.setattr(gate.subprocess, "run", lambda *a, done=done, **k: done)
        assert gate.validate_cv() == expected
