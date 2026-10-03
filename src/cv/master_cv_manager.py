"""Create, load, validate, update and persist the Master CV.

CLI:
    python -m src.cv.master_cv_manager validate data/master_cv.json
    python -m src.cv.master_cv_manager import cv.md --out data/master_cv.json
    python -m src.cv.master_cv_manager export-schema
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from src.core.config import PROJECT_ROOT, get_settings
from src.core.llm_provider import LLMProvider, get_llm_provider
from src.cv.models import MasterCV

SCHEMA_PATH = PROJECT_ROOT / ".agent" / "skills" / "cv_writer" / "master_cv_schema.json"

PARSE_SYSTEM = """You convert a person's raw CV (plain text or Markdown) into a structured \
Master CV. Rules:
- Transcribe facts only. Never invent employers, dates, titles, metrics or skills.
- Dates are YYYY-MM. If only a year is given, use YYYY-01 and keep the original year.
- Give every experience, project and bullet a short, unique, lowercase id \
(e.g. "acme", "acme-1", "acme-2").
- Put numbers that appear in a bullet (percentages, money, counts, durations) in `metrics`, \
copied verbatim.
- List in `skills` only the skills the bullet itself evidences."""


def load(path: Path) -> MasterCV:
    """Load and validate a Master CV JSON file."""
    return MasterCV.model_validate_json(path.read_text(encoding="utf-8"))


def save(cv: MasterCV, path: Path) -> Path:
    """Write `cv` to `path` atomically, keeping the previous version as `<name>.bak`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(cv.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def from_text(raw: str, llm: LLMProvider) -> MasterCV:
    """Parse a raw text/Markdown CV into a validated `MasterCV` via the LLM."""
    return llm.generate(system=PARSE_SYSTEM, prompt=raw, output_model=MasterCV)


def update(cv: MasterCV, patch: dict[str, Any]) -> MasterCV:
    """Apply a JSON merge patch (RFC 7386 semantics) and re-validate.

    Lists are replaced wholesale. To edit one experience, send the full list.
    """
    merged = merge_patch(cv.model_dump(mode="json"), patch)
    return MasterCV.model_validate(merged)


def merge_patch(base: Any, patch: Any) -> Any:
    """RFC 7386 merge: dicts merge recursively, null deletes, anything else (lists) replaces."""
    if not isinstance(patch, dict) or not isinstance(base, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(base.get(key), value)
    return out


def json_schema() -> dict[str, Any]:
    """The JSON Schema for `MasterCV`, as committed to the cv_writer skill."""
    schema = MasterCV.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "master_cv_schema.json"
    return schema


def export_schema(path: Path = SCHEMA_PATH) -> Path:
    """Regenerate the committed JSON Schema from the Pydantic model."""
    path.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Master CV management")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("path", type=Path)
    i = sub.add_parser("import")
    i.add_argument("source", type=Path, help="Raw .txt/.md CV")
    i.add_argument("--out", type=Path, default=None)
    sub.add_parser("export-schema")
    args = parser.parse_args()

    if args.cmd == "validate":
        cv = load(args.path)
        print(f"OK: {cv.basics.name}, {len(cv.experience)} roles, {len(cv.all_skills())} skills")
    elif args.cmd == "import":
        cv = from_text(args.source.read_text(encoding="utf-8"), get_llm_provider())
        out = save(cv, args.out or get_settings().master_cv_path)
        print(f"Wrote {out}. Review it for accuracy before tailoring.")
    elif args.cmd == "export-schema":
        print(f"Wrote {export_schema()}")


if __name__ == "__main__":
    main()
