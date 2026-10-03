"""Check an exported .docx the way an applicant-tracking system reads it.

ATS parsers read the file's text, not its look: contact details must be plain text in the
body (not in headers, text boxes or icons), keywords must be present as words, and tables
can scramble the reading order. The page count is estimated from the word count.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document

from src.cv.models import ATSReport, MasterCV
from src.tools.search_tools import mentions

WORDS_PER_PAGE = 500  # a dense single-column CV page
MAX_PAGES = 2


def docx_text(path: Path) -> tuple[str, int]:
    """Body text as a parser sees it (paragraphs, then table cells) and the table count."""
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts += [cell.text for row in table.rows for cell in row.cells]
    return "\n".join(parts), len(doc.tables)


def _header_text(path: Path) -> str:
    doc = Document(str(path))
    return "\n".join(p.text for s in doc.sections for p in s.header.paragraphs)


def check_docx(path: Path, cv: MasterCV, keywords: list[str] | None = None) -> ATSReport:
    """Read `path` back and report what an ATS would miss (`keywords`: the job's)."""
    text, tables = docx_text(path)
    b = cv.basics
    wanted = {"name": b.name, "email": b.email, "phone": b.phone}
    missing = [k for k, v in wanted.items() if v and v not in text]
    keywords = list(dict.fromkeys(keywords or []))
    absent = [k for k in keywords if not mentions(text, k)]
    words = len(text.split())
    pages = round(words / WORDS_PER_PAGE, 1)
    warnings: list[str] = []
    if missing:
        warnings.append(f"Contact details not readable as text: {', '.join(missing)}")
    if tables:
        warnings.append(f"{tables} table(s): some ATS parsers scramble their reading order")
    if pages > MAX_PAGES:
        warnings.append(f"About {pages} pages: trim to {MAX_PAGES} for most roles")
    if any(v and v in _header_text(path) for v in wanted.values()):
        warnings.append("Contact details in the page header: many ATS parsers skip headers")
    return ATSReport(
        words=words,
        est_pages=pages,
        contact_missing=missing,
        keyword_coverage=(len(keywords) - len(absent)) / len(keywords) if keywords else 1.0,
        missing_keywords=absent,
        warnings=warnings,
    )
