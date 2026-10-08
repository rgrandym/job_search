"""Write a tailored CV as a copy of the candidate's own Word CV, keeping its design.

Every tailored CV looks exactly like the original: same layout, fonts, name, title, section
headings and publications. Only content changes: guarded bullet rewrites, the order of a
role's listed bullets, the order of skills, the summary, and lines left out when trimming for
a more junior role was allowed. Paragraphs move whole, so each keeps its own formatting.

Each role is found as a block in the file (its heading line up to the next role or section),
and `align_to_document` gives the structured CV the document's own wording for those lines
before tailoring, so rewrites start from every fact the user wrote and every line can be
placed exactly. Anything that cannot be placed safely is left as it is and reported.
"""

from __future__ import annotations

import math
import re
import shutil
import uuid
from collections.abc import Sequence
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocxDocument
from docx.text.paragraph import Paragraph

from src.cv.models import Bullet, Experience, MasterCV, SkillGroup
from src.tools.docx_tools import (
    all_paragraphs,
    find_paragraph,
    insert_paragraph_after,
    remove_paragraph,
    reorder_paragraphs,
    replace_text_after,
    set_paragraph_text,
)
from src.tools.search_tools import mentions

SEPARATORS = (", ", "; ", " · ", " | ", " • ", " / ")
MIN_LINE = 40  # shorter lines in a role block are dates, places or labels, not content
MATCH = 0.4  # within one role block, enough to pair a CV bullet with its line
SECTION_NAMES = {
    "education", "publications", "skills", "certifications", "awards", "languages", "patents",
    "presentations", "grants", "references", "interests", "projects", "volunteering",
    "experience", "employment", "profile", "summary", "training", "memberships",
}  # fmt: skip
_STOP = {"the", "and", "ltd", "inc", "llc", "plc", "gmbh", "of", "for", "pre", "seed"}
PROFILE_WORDS = {"profile", "summary", "statement", "about"}
DATE_LINE = re.compile(r"\d{1,2}\s+[A-Z][a-z]+\s+\d{4}")
HEADLINE_MATCH = 0.6  # enough to take the file's own headline line as the CV's headline


def _plain(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _plain(text)) if len(w) > 2 and w not in _STOP}


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _plain(a), _plain(b)).ratio()


def _heading_level(paragraph: Paragraph) -> int | None:
    name = paragraph.style.name if paragraph.style is not None else ""
    if name == "Title":
        return 0
    match = re.fullmatch(r"Heading (\d)", name)
    return int(match.group(1)) if match else None


def _section_break(paragraph: Paragraph) -> bool:
    text = paragraph.text.strip()
    short = 0 < len(text) < 40
    return short and (text.isupper() or _plain(text).rstrip(":") in SECTION_NAMES)


def _is_header(paragraph: Paragraph, role: Experience, by_company: bool) -> bool:
    text = paragraph.text
    if not text.strip() or len(text) > 220:
        return False
    if not by_company:
        return _plain(role.title) in _plain(text)
    company = _words(role.company)
    return bool(company) and len(company & _words(text)) >= 0.6 * len(company)


def _headers(paragraphs: list[Paragraph], cv: MasterCV) -> list[tuple[str, int]]:
    """(role id, index of its heading line), in document order. Tried in turn: a heading
    naming the employer, any line naming it, a heading with the job title, any such line."""
    found: list[tuple[str, int]] = []
    after = -1
    for role in cv.experience:
        later = range(after + 1, len(paragraphs))
        tries = [(True, True), (False, True), (True, False), (False, False)]
        index = None
        for styled_only, by_company in tries:
            index = next(
                (
                    i
                    for i in later
                    if (not styled_only or _heading_level(paragraphs[i]) is not None)
                    and _is_header(paragraphs[i], role, by_company)
                ),
                None,
            )
            if index is not None:
                break
        if index is not None:
            found.append((role.id, index))
            after = index
    return found


def role_blocks(doc: DocxDocument, cv: MasterCV) -> dict[str, list[Paragraph]]:
    """Each role's content lines: from its heading to the next role or section."""
    paragraphs = all_paragraphs(doc)
    headers = _headers(paragraphs, cv)
    blocks: dict[str, list[Paragraph]] = {}
    for number, (role_id, start) in enumerate(headers):
        stop = headers[number + 1][1] if number + 1 < len(headers) else len(paragraphs)
        level = _heading_level(paragraphs[start])
        for index in range(start + 1, stop):
            here = _heading_level(paragraphs[index])
            ends = here is not None and (level is None or here <= level)
            if ends or _section_break(paragraphs[index]):
                stop = index
                break
        lines = paragraphs[start + 1 : stop]
        blocks[role_id] = [p for p in lines if len(p.text.strip()) >= MIN_LINE]
    return blocks


def _pair(bullets: list[Bullet], lines: list[Paragraph]) -> dict[str, Paragraph]:
    """Pair each CV bullet with the block line it came from (best matches first)."""
    scored = sorted(
        ((_ratio(b.text, p.text), n, b.id, p) for b in bullets for n, p in enumerate(lines)),
        key=lambda item: (-item[0], item[1]),
    )
    pairs: dict[str, Paragraph] = {}
    used: set[int] = set()
    for ratio, _, bullet_id, paragraph in scored:
        if ratio < MATCH or bullet_id in pairs or id(paragraph._p) in used:
            continue
        pairs[bullet_id] = paragraph
        used.add(id(paragraph._p))
    return pairs


def profile_paragraphs(doc: DocxDocument) -> list[Paragraph]:
    """The summary as written in the file: the content lines under a profile/summary heading,
    up to the next heading or section."""
    paragraphs = all_paragraphs(doc)
    start = next(
        (
            i
            for i, p in enumerate(paragraphs)
            if (_heading_level(p) is not None or _section_break(p))
            and _words(p.text) & PROFILE_WORDS
        ),
        None,
    )
    if start is None:
        return []
    lines: list[Paragraph] = []
    for p in paragraphs[start + 1 :]:
        if _heading_level(p) is not None or _section_break(p):
            break
        if len(p.text.strip()) >= MIN_LINE:
            lines.append(p)
    return lines


def _covered(bullet: Bullet, lines: list[str]) -> bool:
    words = _words(bullet.text)
    return bool(words) and any(len(words & _words(line)) >= 0.8 * len(words) for line in lines)


def align_to_document(original: Path, cv: MasterCV) -> MasterCV:
    """The CV with each role's bullets as written in the Word file: matched bullets keep
    their ids and take the line's full text; lines the CV missed are added (they are the
    user's own words); CV bullets already covered by a line are folded into it."""
    doc = Document(str(original))
    blocks = role_blocks(doc, cv)
    out = cv.model_copy(deep=True)
    if profile := profile_paragraphs(doc):
        out.basics.summary = "\n\n".join(p.text.strip() for p in profile)
    if out.basics.headline and (line := find_paragraph(doc, out.basics.headline, HEADLINE_MATCH)):
        out.basics.headline = line.text.strip()
    for role in out.experience:
        lines = blocks.get(role.id)
        if not lines:
            continue
        pairs = _pair(role.bullets, lines)
        by_line = {id(p._p): bullet_id for bullet_id, p in pairs.items()}
        old = {b.id: b for b in role.bullets}
        bullets = []
        for line in lines:
            bullet_id = by_line.get(id(line._p)) or f"{role.id}-doc-{uuid.uuid4().hex[:6]}"
            known = old.get(bullet_id)
            bullets.append(
                Bullet(
                    id=bullet_id,
                    text=line.text.strip(),
                    skills=known.skills if known else [],
                    metrics=known.metrics if known else [],
                )
            )
        texts = [line.text for line in lines]
        bullets += [b for b in role.bullets if b.id not in pairs and not _covered(b, texts)]
        role.bullets = bullets
    return MasterCV.model_validate(out.model_dump())


def write_like_original(
    original: Path,
    source: MasterCV,
    tailored: MasterCV,
    out: Path,
    headline_options: Sequence[str] = (),
) -> list[str]:
    """Save `tailored` as a copy of `original` (the Word file `source` was read from) at
    `out`; returns notes on anything kept as in the original. `headline_options` are written
    under the CV's own headline, in its formatting, for the user to keep or delete."""
    doc = Document(str(original))
    claimed: list[Any] = []
    headline = find_paragraph(doc, source.basics.headline or "", 0.75)
    notes = _replace(doc, source.basics.headline, tailored.basics.headline, "Headline", claimed)
    notes += _summary(doc, source.basics.summary, tailored.basics.summary, claimed)
    notes += _footers(doc, tailored.basics.name)
    blocks = role_blocks(doc, source)
    for role in source.experience:
        new = next((r for r in tailored.experience if r.id == role.id), None)
        if new is None:
            continue
        if role.id not in blocks:
            notes.append(f"{role.company}: role not found in the Word file, kept as is")
            continue
        notes += _role(role, new, blocks[role.id])
        claimed += [p._p for p in blocks[role.id]]
    notes += _skills(doc, source.skills, tailored.skills, claimed)
    # Last, so the added lines are never taken for a role, summary or skills line.
    notes += _headline_options(headline, tailored.basics.headline, headline_options)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return notes


def restore_normalized_indents(original: Path, edited: Path) -> bool:
    """Undo a DOCX editor's mass indent normalization without changing edited wording.

    Some viewers discard explicit zero first-line indents and add direct left indents when
    saving. Restore only that distinctive, document-wide change, and only when the source
    and edited files still have matching paragraph positions, styles and alignments.
    """
    source = Document(str(original)).paragraphs
    document = Document(str(edited))
    current = document.paragraphs
    if len(source) != len(current):
        return False
    drift: list[tuple[Paragraph, Paragraph]] = []
    for before, after in zip(source, current, strict=True):
        old_style = before.style.name if before.style is not None else ""
        new_style = after.style.name if after.style is not None else ""
        if old_style != new_style or before.alignment != after.alignment:
            return False
        old, new = before.paragraph_format, after.paragraph_format
        left_changed = old.left_indent != new.left_indent
        first_changed = old.first_line_indent != new.first_line_indent
        if left_changed and not (old.left_indent is None and new.left_indent is not None):
            return False
        if first_changed and not (old.first_line_indent == 0 and new.first_line_indent is None):
            return False
        if left_changed or first_changed:
            drift.append((before, after))
    if len(drift) < max(8, len(source) // 3):
        return False
    for before, after in drift:
        after.paragraph_format.left_indent = before.paragraph_format.left_indent
        after.paragraph_format.first_line_indent = before.paragraph_format.first_line_indent
    backup = edited.with_name(f".{edited.name}.before-indent-repair")
    if not backup.exists():
        shutil.copy2(edited, backup)
    temporary = edited.with_name(f".{edited.name}.{uuid.uuid4().hex}.tmp")
    try:
        document.save(str(temporary))
        temporary.replace(edited)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _replace(
    doc: DocxDocument, old: str | None, new: str | None, label: str, claimed: list[Any]
) -> list[str]:
    if not old or not new or old == new:
        return []
    paragraph = find_paragraph(doc, old, 0.75, claimed)
    if paragraph is None:
        return [f"{label} kept as in your CV (not found as one paragraph in the Word file)"]
    set_paragraph_text(paragraph, new)
    claimed.append(paragraph._p)
    return []


def _headline_options(
    line: Paragraph | None, headline: str | None, options: Sequence[str]
) -> list[str]:
    """Each suggested headline as a copy of the headline `line`, placed under it in order."""
    extra = [o for o in dict.fromkeys(options) if o.strip() and o != headline]
    if not extra:
        return []
    if line is None:
        return ["Suggested headlines not added (headline not found as one line in the Word file)"]
    for option in reversed(extra):
        insert_paragraph_after(line, option)
    return []


def _summary(doc: DocxDocument, old: str | None, new: str | None, claimed: list[Any]) -> list[str]:
    """Write the summary into the file's profile lines (as aligned): one paragraph each when
    the counts match, otherwise all of it in the first and the rest removed."""
    lines = profile_paragraphs(doc)
    if not old or not new or old == new:
        return []
    if not lines or "\n\n".join(p.text.strip() for p in lines) != old:
        return _replace(doc, old, new, "Summary", claimed)
    parts = [part.strip() for part in new.split("\n\n") if part.strip()]
    if len(parts) != len(lines):
        parts = [" ".join(parts)]
    for paragraph, text in zip(lines, parts, strict=False):
        set_paragraph_text(paragraph, text)
        claimed.append(paragraph._p)
    for paragraph in lines[len(parts) :]:
        remove_paragraph(paragraph)
    return []


def _footers(doc: DocxDocument, name: str) -> list[str]:
    """Headers and footers carry the CV's own name, say "CV" (not "Master CV") and today's
    date. Changed only where the text sits in one run, so page fields stay intact."""
    notes: list[str] = []
    today = f"{date.today().day} {date.today():%B %Y}"
    for section in doc.sections:
        headers = (section.header, section.first_page_header, section.even_page_header)
        footers = (section.footer, section.first_page_footer, section.even_page_footer)
        for part in (*headers, *footers):
            if part.is_linked_to_previous:
                continue
            for paragraph in part.paragraphs:
                for segment in re.split(r"\s+[|·•–—]\s+", paragraph.text):
                    new = _footer_segment(segment.strip(), name, today)
                    if new is None or new == segment.strip():
                        continue
                    run = next((r for r in paragraph.runs if segment.strip() in r.text), None)
                    if run is None:
                        notes.append(f"Footer kept as in your CV: {segment.strip()!r}")
                        continue
                    run.text = run.text.replace(segment.strip(), new, 1)
    return list(dict.fromkeys(notes))


def _footer_segment(segment: str, name: str, today: str) -> str | None:
    """The replacement for one "a | b | c" segment of a header or footer, or None."""
    if re.fullmatch(r"master\s+cv", segment, re.IGNORECASE):
        return "CV"
    if DATE_LINE.fullmatch(segment):
        return today
    own = [w for w in re.findall(r"[a-z]+", name.casefold()) if len(w) > 2]
    here = set(re.findall(r"[a-z]+", segment.casefold()))
    if set(own) <= here:  # already the full name (or a longer form of it): keep the user's
        return None
    if len(own) >= 2 and own[0] in here and len(here & set(own)) >= 2 and len(segment) < 60:
        return name
    return None


def _listed(paragraph: Paragraph) -> bool:
    """A bullet or numbered line (these move); other lines, such as a role's intro, stay."""
    ppr = paragraph._p.pPr
    numbered = ppr is not None and ppr.numPr is not None
    return numbered or "List" in (paragraph.style.name if paragraph.style is not None else "")


def _contiguous(paragraphs: list[Paragraph]) -> bool:
    """One block: same parent, nothing but these (or blank) paragraphs between them."""
    if len({id(p._p.getparent()) for p in paragraphs}) != 1:
        return False
    children = list(paragraphs[0]._p.getparent())
    own = [children.index(p._p) for p in paragraphs]
    members = {id(p._p) for p in paragraphs}
    between = children[min(own) : max(own) + 1]
    return all(id(e) in members or not "".join(e.itertext()).strip() for e in between)


def _role(old: Experience, new: Experience, lines: list[Paragraph]) -> list[str]:
    found = _pair(old.bullets, lines)
    notes = []
    if missing := len(old.bullets) - len(found):
        notes.append(f"{old.company}: {missing} line(s) not found in the Word file, kept as is")
    originals = {b.id: b.text for b in old.bullets}
    kept = {b.id for b in new.bullets}
    for bullet in new.bullets:
        if bullet.id in found and bullet.text != originals.get(bullet.id):
            set_paragraph_text(found[bullet.id], bullet.text)
    movable = [p for p in lines if _listed(p) and any(p is q for q in found.values())]
    order = [found[b.id] for b in new.bullets if b.id in found and _listed(found[b.id])]
    for bullet_id, paragraph in found.items():
        if bullet_id not in kept and not _listed(paragraph):
            remove_paragraph(paragraph)  # left out (trimming allowed); listed ones go below
    if [id(p) for p in order] == [id(p) for p in movable]:
        return notes
    if movable and _contiguous(movable):
        reorder_paragraphs(movable, order)
    elif movable:
        notes.append(f"{old.company}: bullets are not one block in the Word file; order kept")
    return notes


def _list_paragraph(doc: DocxDocument, group: SkillGroup, claimed: list[Any]) -> Paragraph | None:
    """The paragraph that lists most of `group`'s skills."""
    need = max(2, math.ceil(0.6 * len(group.items)))
    best: tuple[int, Paragraph | None] = (0, None)
    for p in all_paragraphs(doc):
        if any(p._p is e for e in claimed):
            continue
        hits = sum(mentions(p.text, item) for item in group.items)
        if hits >= need and hits > best[0]:
            best = (hits, p)
    return best[1]


def _reorder_list(paragraph: Paragraph, items: list[str]) -> bool:
    """Rewrite "Label: a, b, c." with `items` in the new order, keeping the label's runs.
    Only when the text after the label is exactly those items."""
    text = paragraph.text
    sep = max(SEPARATORS, key=text.count)
    starts = [text.find(i) for i in items if text.find(i) >= 0]
    if not starts or text.count(sep) == 0:
        return False
    prefix = min(starts)
    tail = text[prefix:].rstrip()
    end = "." if tail.endswith(".") else ""
    listed = [part.strip() for part in tail.rstrip(".").split(sep)]
    ordered = [i for i in items if i in listed]
    if sorted(listed) != sorted(ordered):  # something other than these skills is listed
        return False
    if ordered != listed:
        replace_text_after(paragraph, prefix, sep.join(ordered) + end)
    return True


def _skills(
    doc: DocxDocument, old: list[SkillGroup], new: list[SkillGroup], claimed: list[Any]
) -> list[str]:
    """Most relevant skills first within each list, and lists in order of relevance."""
    placed: dict[str, Paragraph] = {}
    kept: list[str] = []
    for group in new:
        paragraph = _list_paragraph(doc, group, claimed)
        if paragraph is not None and _reorder_list(paragraph, group.items):
            placed[group.category] = paragraph
            claimed.append(paragraph._p)
        elif group.items != next((g.items for g in old if g.category == group.category), []):
            kept.append(group.category)
    notes = [f"Skills order kept as in your CV for: {', '.join(kept)}"] if kept else []
    in_old = [placed[g.category] for g in old if g.category in placed]
    in_new = [placed[g.category] for g in new if g.category in placed]
    if len(in_old) > 1 and in_old != in_new and _contiguous(in_old):
        reorder_paragraphs(in_old, in_new)
    return notes
