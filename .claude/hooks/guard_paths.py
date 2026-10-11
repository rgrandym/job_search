#!/usr/bin/env python3
"""PreToolUse hook: refuse edits to the paths CLAUDE.md protects, and say what to do instead.

Reads the hook payload on stdin. A protected path gets a `deny` decision whose reason is an
instruction to the model; anything else passes silently. Never raises: a broken guard must
not block work, so any unexpected error lets the call through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCHEMAS = {
    ".agent/skills/cv_writer/master_cv_schema.json": "src/cv/models.py",
    ".agent/skills/job_search/job_schema.json": "src/jobs/models.py",
}


def _relative(raw: str) -> str | None:
    """The path relative to the repo root, or None when it lies outside the repo."""
    path = Path(raw)
    path = (ROOT / path) if not path.is_absolute() else path
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def reason_for(raw: str) -> str | None:
    """Why `raw` may not be edited, as an instruction to the model; None when it may."""
    rel = _relative(raw)
    if rel is None:
        return None
    if rel == ".env" or rel.startswith(".env."):
        return (
            ".env holds the user's secrets and is protected. Do not edit it; tell the user "
            "which variable to set and why."
        )
    if rel.startswith("data/") and not rel.startswith("data/examples/"):
        return (
            f"{rel} is the user's personal data and is protected. Do not edit it. Change the "
            "code that writes it, use a fixture under data/examples/, or ask the user to make "
            "the change in the app."
        )
    if rel.startswith("output/"):
        return f"{rel} is generated output and is protected. Regenerate it through the app or CLI."
    if rel.startswith("web/dist/"):
        return f"{rel} is a build artefact. Edit web/src and run `cd web && npm run build`."
    if rel in SCHEMAS:
        return (
            f"{rel} is generated. Change {SCHEMAS[rel]} and run the matching "
            "`export-schema` command from CLAUDE.md."
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        reason = reason_for(str(raw)) if raw else None
    except Exception:  # noqa: BLE001  a broken guard must never block work
        return 0
    if reason:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
