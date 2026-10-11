#!/usr/bin/env python3
"""One JSON line describing what the job-search workspace is doing, for live views.

    python3 scripts/live_status.py status [--plans] [--backend URL] [--frontend URL]

The Claude Code pane (`.claude/mods/job-search-live`) polls this every few seconds; anything
else (a terminal, Codex) can call it too, so every view agrees. Standard library only, never
raises (a broken probe reads as "down"), and never prints CV text, keys or email contents:
only counts, task titles and steps, and plan percentages.

Fields: backend / frontend (bool), searches (running), tasks (running app tasks with their
steps), gate (the last `.claude/hooks/gate.py` result), git_dirty (changed paths), and with
`--plans` the plan windows and spent plans from `GET /api/connections`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "http://127.0.0.1:8000"
FRONTEND = "http://localhost:5173"
TIMEOUT_S = 1.5


def _get(url: str) -> Any:
    """Parsed JSON from `url`, or None when it does not answer."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:  # noqa: S310 (local URLs)
            return json.loads(resp.read().decode() or "null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S):  # noqa: S310 (local URLs)
            return True
    except urllib.error.HTTPError:
        return True  # it answered
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _gate() -> dict[str, Any] | None:
    try:
        data = json.loads((ROOT / ".run" / "gate.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git_dirty() -> int:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    return len([line for line in out.stdout.splitlines() if line.strip()])


def _task(t: dict[str, Any]) -> dict[str, Any]:
    keep = ("title", "step", "done", "total", "completed", "waiting_on", "started_at", "now")
    return {k: t.get(k) for k in keep}


def _plans(backend: str) -> list[dict[str, Any]]:
    view = _get(f"{backend}/api/connections")
    if not isinstance(view, dict):
        return []
    out = []
    for c in view.get("connections") or []:
        if c.get("billing") != "plan":
            continue
        windows = (c.get("windows") or {}).get("windows") or []
        out.append(
            {
                "label": c.get("label"),
                "windows": [
                    {k: w.get(k) for k in ("name", "used_percent", "resets_at")} for w in windows
                ],
                "exhausted_until": c.get("exhausted_until"),
            }
        )
    return out


def status(backend: str = BACKEND, frontend: str = FRONTEND, plans: bool = False) -> dict[str, Any]:
    activity = _get(f"{backend}/api/activity")
    up = activity is not None or _up(f"{backend}/api/state")
    out: dict[str, Any] = {
        "backend": up,
        "frontend": _up(frontend),
        "searches": int(activity.get("searches", 0)) if isinstance(activity, dict) else 0,
        "tasks": [_task(t) for t in (activity or {}).get("tasks", [])]
        if isinstance(activity, dict)
        else [],
        "gate": _gate(),
        "git_dirty": _git_dirty(),
    }
    if plans and up:
        out["plans"] = _plans(backend)
    return out


def _arg(argv: list[str], name: str, default: str) -> str:
    return (
        argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "status":
        print("usage: live_status.py status [--plans] [--backend URL] [--frontend URL]")
        return 2
    try:
        data = status(
            _arg(argv, "--backend", BACKEND), _arg(argv, "--frontend", FRONTEND), "--plans" in argv
        )
    except Exception as exc:  # noqa: BLE001 - a live view must always get a line
        data = {"backend": False, "frontend": False, "error": str(exc)}
    print(json.dumps(data, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
