#!/usr/bin/env python3
"""The project's quality gate, run as Claude Code hooks and by hand.

  PostToolUse (Write|Edit|MultiEdit): ruff format + ruff check on the edited Python file, and
      `mypy src` when it lies under src/; `tsc` when it lies under web/src. Failures exit 2 so
      Claude Code feeds them back to the model.
  Stop: `pytest -q` once per new set of Python changes (keyed on the git diff), never twice
      in a row for the same stop (`stop_hook_active`).
  --all: everything, for a person or an agent without hooks (Codex): ruff, mypy, tsc, pytest.
  --validate-cv: does data/master_cv.json still validate? Exit 0 yes (or no structured CV),
      1 no (the field errors are printed, never the CV's content), 2 cannot tell.

Each run writes its result to .run/gate.json, which the live pane reads. Never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".run" / "gate.json"
STOP_STATE = ROOT / ".run" / "stop_gate.json"
PY_DIRS = ("src/", "tests/", ".agent/", "scripts/")
TIMEOUT_S = 300


def env_bin() -> Path | None:
    """The job_search conda env's bin directory, wherever conda lives."""
    candidates = [os.environ.get("JOB_SEARCH_BIN", "")]
    prefix = os.environ.get("CONDA_PREFIX", "")
    if prefix.endswith("job_search"):
        candidates.append(f"{prefix}/bin")
    if conda := os.environ.get("CONDA_EXE"):
        candidates.append(str(Path(conda).parents[1] / "envs/job_search/bin"))
    home = Path.home()
    for base in (home / "miniconda3", home / "anaconda3", Path("/opt/miniconda3")):
        candidates.append(str(base / "envs/job_search/bin"))
    for c in candidates:
        if c and (Path(c) / "ruff").exists():
            return Path(c)
    return None


def tool(name: str) -> str | None:
    found = env_bin()
    return str(found / name) if found and (found / name).exists() else shutil.which(name)


def run(argv: list[str], cwd: Path = ROOT) -> tuple[bool, str]:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{argv[0]}: {exc}"
    text = (out.stdout + out.stderr).strip()
    return out.returncode == 0, text


def checks_for(rel: str) -> list[tuple[str, list[str], Path]]:
    """(label, argv, cwd) of the checks an edit to `rel` needs."""
    if rel.endswith(".py") and rel.startswith(PY_DIRS):
        ruff = tool("ruff") or "ruff"
        out = [("ruff format", [ruff, "format", rel], ROOT), ("ruff", [ruff, "check", rel], ROOT)]
        if rel.startswith("src/"):
            out.append(("mypy", [tool("mypy") or "mypy", "src"], ROOT))
        return out
    if rel.startswith("web/src/") and rel.endswith((".ts", ".tsx")):
        return [("tsc", ["npx", "tsc", "-b", "--noEmit"], ROOT / "web")]
    return []


def all_checks() -> list[tuple[str, list[str], Path]]:
    ruff, mypy, pytest = (tool(n) or n for n in ("ruff", "mypy", "pytest"))
    return [
        ("ruff", [ruff, "check", "src", "tests", ".agent"], ROOT),
        ("ruff format", [ruff, "format", "--check", "src", "tests", ".agent"], ROOT),
        ("mypy", [mypy, "src"], ROOT),
        ("tsc", ["npx", "tsc", "-b", "--noEmit"], ROOT / "web"),
        ("pytest", [pytest, "-q"], ROOT),
    ]


def record(kind: str, results: list[tuple[str, bool, str]]) -> None:
    try:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(
            json.dumps(
                {
                    "kind": kind,
                    "at": time.time(),
                    "ok": all(ok for _, ok, _ in results),
                    "checks": [{"name": n, "ok": ok} for n, ok, _ in results],
                }
            )
        )
    except OSError:
        pass


def execute(kind: str, checks: list[tuple[str, list[str], Path]]) -> int:
    results = [(label, *run(argv, cwd)) for label, argv, cwd in checks]
    if results:
        record(kind, results)
    failed = [(label, text) for label, ok, text in results if not ok]
    if not failed:
        return 0
    tail = "\n\n".join(f"{label} failed:\n{text[-3000:]}" for label, text in failed)
    print(f"The quality gate failed. Fix these before continuing.\n\n{tail}", file=sys.stderr)
    return 2


def _relative(raw: str) -> str | None:
    path = Path(raw)
    path = (ROOT / path) if not path.is_absolute() else path
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def diff_key() -> str | None:
    """A hash of the uncommitted Python changes; None when there are none."""
    ok, names = run(["git", "status", "--porcelain", "--", "*.py"])
    if not ok or not names.strip():
        return None
    _, diff = run(["git", "diff", "--", "*.py"])
    return hashlib.sha256((names + diff).encode()).hexdigest()


def on_stop(payload: dict[str, object]) -> int:
    if payload.get("stop_hook_active"):
        return 0  # already sent back once for this stop
    key = diff_key()
    if key is None:
        return 0
    try:
        if json.loads(STOP_STATE.read_text()).get("key") == key:
            return 0  # these exact changes were already tested
    except (OSError, ValueError):
        pass
    code = execute("stop", [("pytest", [tool("pytest") or "pytest", "-q", "-x"], ROOT)])
    try:
        STOP_STATE.parent.mkdir(exist_ok=True)
        STOP_STATE.write_text(json.dumps({"key": key, "ok": code == 0}))
    except OSError:
        pass
    return code


VALIDATE = (
    "import sys\n"
    "from pydantic import ValidationError\n"
    "from src.cv.master_cv_manager import load\n"
    "try:\n"
    "    load(__import__('pathlib').Path('data/master_cv.json'))\n"
    "except ValidationError as exc:\n"
    "    for err in exc.errors()[:8]:\n"
    "        print('.'.join(map(str, err['loc'])) + ': ' + err['msg'])\n"
    "    sys.exit(1)\n"
)


def validate_cv() -> int:
    """0: the Master CV validates (or there is none); 1: it does not; 2: cannot tell."""
    if not (ROOT / "data" / "master_cv.json").exists():
        print("No structured Master CV (data/master_cv.json); nothing to validate.")
        return 0
    python = tool("python") or "python3"
    try:
        out = subprocess.run(
            [python, "-c", VALIDATE], cwd=ROOT, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Could not run the validation: {exc}")
        return 2
    if out.returncode == 1:
        print(f"data/master_cv.json does not validate:\n{out.stdout.strip()}")
        return 1
    if out.returncode != 0:
        print(f"Could not tell whether data/master_cv.json validates: {out.stderr.strip()[-300:]}")
        return 2
    print("data/master_cv.json validates.")
    return 0


def main(argv: list[str]) -> int:
    if "--all" in argv:
        return execute("all", all_checks())
    if "--validate-cv" in argv:
        return validate_cv()
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        if payload.get("hook_event_name") == "Stop":
            return on_stop(payload)
        raw = (payload.get("tool_input") or {}).get("file_path") or ""
        rel = _relative(str(raw)) if raw else None
        return execute("edit", checks_for(rel)) if rel else 0
    except Exception as exc:  # noqa: BLE001  the gate must never crash the session
        print(f"gate.py: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
