"""cv_writer skill entrypoint: render a Master/Tailored CV JSON to .docx.

Thin CLI over `src.cv.docx_exporter`. Templates are defined there (`TEMPLATES`).
Add or change templates in `src/`, not here.

Usage (from the repo root):
    python .agent/skills/cv_writer/docx_templates.py --list
    python .agent/skills/cv_writer/docx_templates.py --cv output/tailored_acme.json \
        --template classic --out output/Jane_Doe_Acme.docx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.cv.docx_exporter import TEMPLATES, export_docx  # noqa: E402
from src.cv.models import MasterCV, TailoredCV  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="List available templates")
    parser.add_argument("--cv", type=Path, help="MasterCV or TailoredCV JSON")
    parser.add_argument("--template", default="classic", choices=sorted(TEMPLATES))
    parser.add_argument("--out", type=Path, help="Output .docx path")
    args = parser.parse_args()

    if args.list:
        for name, tpl in TEMPLATES.items():
            print(
                f"{name:10s} font={tpl.font} body={tpl.body_pt}pt accent={tpl.accent} "
                f"sections={','.join(tpl.sections)}"
            )
        return
    if not args.cv or not args.out:
        parser.error("--cv and --out are required unless --list is given")

    data = json.loads(args.cv.read_text(encoding="utf-8"))
    cv: MasterCV | TailoredCV = (
        TailoredCV.model_validate(data) if "cv" in data else MasterCV.model_validate(data)
    )
    print(f"Wrote {export_docx(cv, args.out, args.template)}")


if __name__ == "__main__":
    main()
