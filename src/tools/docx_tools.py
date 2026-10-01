"""Low-level python-docx helpers. No CV knowledge here, only document primitives."""

from __future__ import annotations

from docx.document import Document as DocxDocument
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph


def hex_to_rgb(color: str) -> RGBColor:
    """'#1F3864' -> RGBColor."""
    return RGBColor.from_string(color.lstrip("#").upper())


def set_margins(doc: DocxDocument, inches: float) -> None:
    """Apply equal page margins to every section."""
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Inches(inches)
        section.left_margin = section.right_margin = Inches(inches)


def set_base_font(doc: DocxDocument, name: str, size_pt: float) -> None:
    """Set the Normal style font (including East-Asian fallback so Word honours it)."""
    style = doc.styles["Normal"]
    style.font.name = name
    style.font.size = Pt(size_pt)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), name)
    pf = style.paragraph_format
    pf.space_after = Pt(2)
    pf.space_before = Pt(0)


def add_bottom_border(paragraph: Paragraph, color: str = "808080", size: int = 6) -> None:
    """Draw a horizontal rule under a paragraph."""
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color.lstrip("#"))
    borders.append(bottom)
    ppr.append(borders)


def add_text(
    doc: DocxDocument,
    text: str,
    *,
    size_pt: float | None = None,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    align: int | None = None,
    space_after_pt: float | None = None,
) -> Paragraph:
    """Add a single-run paragraph."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold, run.italic = bold, italic
    if size_pt:
        run.font.size = Pt(size_pt)
    if color:
        run.font.color.rgb = hex_to_rgb(color)
    if align is not None:
        p.alignment = align  # type: ignore[assignment]
    if space_after_pt is not None:
        p.paragraph_format.space_after = Pt(space_after_pt)
    return p


def add_split_line(
    doc: DocxDocument, left: str, right: str, *, bold_left: bool = True, right_tab_in: float = 7.0
) -> Paragraph:
    """One line with `left` text and `right` text flush to a right tab stop."""
    p = doc.add_paragraph()
    p.paragraph_format.tab_stops.add_tab_stop(Inches(right_tab_in), WD_TAB_ALIGNMENT.RIGHT)
    p.paragraph_format.space_before = Pt(4)
    p.add_run(left).bold = bold_left
    if right:
        p.add_run(f"\t{right}")
    return p


def add_bullet(doc: DocxDocument, text: str) -> Paragraph:
    """Add a 'List Bullet' paragraph (ATS-friendly native Word list)."""
    p = doc.add_paragraph(text, style="List Bullet")
    p.paragraph_format.space_after = Pt(1)
    return p
