---
name: cv_writer
description: Create or update the structured Master CV (including evidence from documents the user supplies, accepted one by one), tailor it to a job description with ATS keywords and STAR-format bullets (no fabricated facts; reviewed and revised once), write guarded cover letters, and export them to styled Word (.docx) documents with an ATS read-back. Use when the user wants to import/edit their CV, tailor a CV to a job, write a cover letter, or produce a .docx CV.
---

# cv_writer

Turns a factual **Master CV** into job-specific, ATS-optimised CVs and exports them to `.docx`.

| File | Role |
| --- | --- |
| `master_cv_schema.json` | JSON Schema of the Master CV. **Generated** from `src/cv/models.py::MasterCV`. Never hand-edit. |
| `docx_templates.py` | CLI to render a CV JSON to `.docx` with a named template. |
| `src/cv/master_cv_manager.py` | load / save / validate / import / merge-patch the Master CV |
| `src/cv/tailor.py` | JD analysis → tailoring plan (LLM) → guarded application (deterministic) → review → one revision |
| `src/cv/cover_letter.py` | Cover-letter draft (LLM) → guarded paragraphs (deterministic) |
| `src/services/tailored_documents.py` | Saved tailored drafts, guarded edits and Word versions |
| `src/services/cover_letters.py` | Saved letter text, edits and Word/text exports |
| `src/cv/ats.py` | Reads the exported .docx back the way an ATS does |
| `src/cv/docx_original.py` | Tailored CV written as a copy of the original Word CV (its design kept) |
| `src/cv/docx_exporter.py` | Word rendering for PDF/text CVs and cover letters + template definitions (`TEMPLATES`) |
| `src/services/enrichment.py` | Evidence review queue: document → proposed CV additions → user accepts |

The Master CV lives at `data/master_cv.json` (git-ignored, personal data). Example:
`data/examples/master_cv.example.json`.
The Available CVs panel can delete a CV and its parsed copy, stored profile summaries and
career intent. Deleting a generated Word CV also removes its saved tailored draft record;
application history and cover letters remain. Each deletion requires confirmation in the UI.
Clicking a CV selects it and opens its structured contents in a resizable editor. An uploaded
source is parsed on first open, then reviewed edits are saved to its parsed copy; the original
PDF, Word or text file stays unchanged.

---

## 1. Create or update the Master CV

The web UI first stores uploaded files unchanged in `data/cvs/`. Uploading and selecting a CV
must not parse it or call an LLM. Conversion to the structured Master CV happens on demand when
the user requests a CV-powered action such as profile matching, summarisation, or tailoring.

**From raw text / Markdown:**
```bash
python -m src.cv.master_cv_manager import path/to/cv.md --out data/master_cv.json
python -m src.cv.master_cv_manager validate data/master_cv.json
```
After import, **show the user what was extracted and ask them to confirm** dates, titles and
metrics. The LLM transcribes. It must not embellish.

**Editing by hand or by agent:** edit the JSON, then re-run `validate`. In code, use
`master_cv_manager.update(cv, patch)` (RFC 7386 merge patch: `null` deletes a key, lists are
replaced whole) followed by `save()`, which keeps a `.bak`.

**Schema rules that matter:**
- `start` / `end` are `YYYY-MM`. `end: null` means current role.
- Every experience, project and bullet has a unique lowercase `id`. Tailoring uses bullet
  ids for provenance, so **never renumber existing ids**. Append new ones.
- `bullets[].metrics` holds the verbatim numbers in that bullet. `bullets[].skills` holds only
  the skills that bullet evidences. These two lists are what the anti-fabrication guard trusts.
- `preferences` (target titles, locations, work arrangements, relocation) feeds job search
  and is never printed on the CV.

If you change `MasterCV`, regenerate the schema: `python -m src.cv.master_cv_manager export-schema`.

**Evidence from other documents** (`services/enrichment.py`; Profiles › "Add evidence";
`POST /api/evidence/upload|text`, `GET /api/evidence`, `POST /api/evidence/{id}`): the user
uploads or pastes a document (portfolio page, project report, publication list, reference
letter, certificate). The LLM proposes skills, certifications, projects and achievement
bullets (for a role the CV has) with a verbatim quote and a confidence. Code drops any
proposal whose quote is not in the document, whose numbers are not in its quote, or that the
CV already has. Proposals wait in `data/evidence_queue.json`; **only the ones the user accepts**
(optionally reworded) are written to the selected Master CV: skills under "Additional",
bullets with fresh ids and their numbers as `metrics`. Inferred items stay suggestions.

---

## 2. Analyse the job description

`tailor.analyze_jd(jd_text, llm) -> JDAnalysis`

1. **Identify the role**: `job_title`, `company`, `seniority`
   (intern · junior · mid · senior · staff · principal · manager · director · executive).
2. **Hard keywords** (`hard_skills`): concrete, ATS-matchable nouns: languages, frameworks,
   platforms, methods, domains, certifications. Use canonical names ("Kubernetes", not "k8s").
3. **Soft keywords** (`soft_skills`): stakeholder management, mentoring, ownership, …
4. **Priority split**: `must_have` (required / "you have") vs `nice_to_have`
   (preferred / bonus). Use the JD's own words. Do not infer requirements it doesn't state.
5. **Responsibilities**: the verbs and outcomes the role is accountable for. These shape
   which bullets get promoted.

---

## 3. Tailoring rules: same CV, content fitted to the post

These hold for every tailored CV (user's decision; mandatory):

1. **Same format and design as the original CV** (template `"original"`, the default).
   `docx_original.write_like_original` writes the tailored CV as a copy of the selected Word
   CV, or, for a PDF or text CV, of its Word version in the library
   (`cv_service.original_docx`: the uploaded `.docx` with the same name, or at least 60% the
   same words); for the structured Master CV, the library Word CV that holds at least 60% of
   its words (`_master_docx`). Layout, fonts, name, section headings and publications stay exactly as in the
   original. Only the content changes: bullet wording, bullet order within each role, skill
   order, the summary. Paragraphs move whole, so each keeps its own formatting. The `classic` /
   `modern` / `compact` templates are used only when the user picks one, or when no Word
   version exists (`TailoredCV.document_notes` says which).
2. **The CV keeps its own headline; publications are never edited.** The plan returns up to 4
   `headline_options` for the job; each must pass the headline guard (no level above any title
   held, no role the CV does not show, no unevidenced skills or numbers) and the passing ones
   are offered in `TailoredCV.headline_options` and written into the Word file under the CV's
   own headline, in its formatting, so the user can delete the ones they do not want. Picking
   one in the app saves a new Word version with only that headline, through the guarded edit
   (`tailored_documents.edit`). The second reader sees the CV as printed (no per-bullet skill
   tags) and is told these rules, so it suggests rewording and reordering only.
3. **Never lose information.** The bullets that fit the post move to the top of their role and
   the others follow at the bottom of that role; relevant skills lead each skills list and the
   rest follow. Nothing is removed, because the rest may still be useful. Before tailoring a
   Word CV, `docx_original.align_to_document` finds each role's block in the file (its heading
   line to the next role or section) and gives the structured CV the document's own wording
   (matched bullets keep their ids; lines the parse missed are added), so rewrites start from
   every fact the user wrote. The summary is read the same way, from the lines under the
   file's profile/summary heading (several paragraphs joined by a blank line), and the
   rewritten summary goes back into those lines (`docx_original.profile_paragraphs`); the
   headline is taken from its line in the file. Headers and footers get the CV's own name,
   "CV" instead of "Master CV" and today's date. A rewrite that drops one of its line's numbers or cuts it below
   60% of its length is rejected (`tailor._loss_reason`). Only bullet and numbered lines move;
   a role's intro paragraph and date lines stay in place.
4. **Length is the user's choice** (`length`, a picker on the job card): `full` keeps every
   line; `junior` trims for a junior role; `auto` (default) trims only for a clearly more junior
   role, which `tailor.assess_trim(master, jd, jd_text, length)` decides in code, never the
   model: the job's stated level (`JDAnalysis.seniority`) at least two steps below the highest
   title held, or one step below while the posting asks for at most half the candidate's years
   of experience. A job with no stated level is never trimmed automatically.
   When allowed, the plan may leave out bullets that would overstate the candidate, keeping at
   least two per role; roles, education and publications always stay; a left-out bullet that
   carried a JD keyword is put back; every left-out line is recorded in `TailoredCV.changes`
   and listed in the UI with the reason (`TailoredCV.trim`).
5. Anything the Word writer cannot find safely in the file is left as in the original and
   reported in `TailoredCV.document_notes`.

## 3a. Rewrite bullets as STAR accomplishments, without hallucinating

`tailor.propose_plan(..., guidance) -> TailoringPlan`, then `tailor.apply_plan(...)` enforces
the rules. `guidance` is the job_matcher's verdict on this job (fit summary, reasons,
transferable evidence, gaps), passed by `cv_service.tailor_to_job` so the plan leads with
what the matcher found and never papers over its gaps.

For each relevant Master CV bullet, produce one `RewrittenBullet` with `source_id` = that
bullet's id. Compress STAR into one sentence:

> **Action verb** + *Situation/Task context* + **what you did (using JD terminology)** + *measurable Result*

| Source bullet | Tailored (JD asks for "MLOps", "cost optimisation") |
| --- | --- |
| Led migration of model training pipelines to Kubernetes, cutting training costs by 35%. | Drove MLOps migration of model training pipelines to Kubernetes, cutting training costs 35%. |

**Allowed:** rephrasing, reordering clauses, swapping in the JD's synonym for the *same* thing,
dropping irrelevant detail within a bullet, promoting relevant bullets (the rest move down).
Omitting bullets only under rule 4 above.

**Forbidden, and auto-rejected by `apply_plan`:**
- Any number, %, currency amount or count not present in the source bullet or its `metrics`.
- Any skill (JD hard skill / must-have, a CV skill, or a known technology) the source bullet
  does not evidence in its text or `skills` list, even if the CV shows it in another role.
- Unknown `source_id`s, or merging facts from two bullets into one.
- Inventing employers, titles, dates, team sizes, or scope ("global", "company-wide") the source lacks.

Rejected rewrites keep the original bullet. Every decision is logged in `TailoredCV.changes`
(`accepted`, `reason`). **Report rejected rewrites and `missing_keywords` to the user.** A
missing keyword is a gap to discuss, never something to paper over. If the user confirms they
do have that experience, add it to the **Master CV** first, then re-tailor.

Also produced, and guarded the same way (recorded in `changes` as `summary`):
- Headline suggestions (rule 2), and a user's own headline edit to a saved draft, are rejected
  if they claim a seniority above any title held, a role the CV does not show (role words of the
  title part, before a comma, "|" or dash), or skills or numbers the CV lacks.
- `summary` (3–4 substantive sentences): lead with evidence relevant to the job, then retain
  distinctive relevant breadth from the Master CV, such as leadership, hands-on practice or
  adjacent experience. Numbers and skills must exist somewhere in the CV. The tailoring request
  may emphasise leadership or hands-on work and use a more senior or junior presentation;
  these choices never change the candidate's evidenced level or permit new claims.
- `bullet_order`: a list of `{experience_id, bullet_ids}` for every role (a list, because
  strict output schemas such as Codex's cannot carry dict keys) (most relevant first; unlisted bullets follow, or are left out only under
  rule 4) and `skills_priority` (re-orders existing skills only). If trimming left out the only
  bullet carrying a JD keyword, the bullet is put back (`restored_keywords`);
  `missing_keywords` are then true gaps. Keywords are `tailor.ats_keywords`: hard skills and
  must-haves of at most 4 words; requirement sentences are left to the job_matcher's verdict.

**Review and revision** (`tailor(..., review=True)`, the default): a second, fresh reader
(`critique_cv`, `CVCritique`) lists JD requirements a Master CV bullet evidences but the
tailored CV leaves out or buries (citing that bullet; points citing unknown ids are dropped),
weak bullets (passive, generic, result buried), and up to 3 order notes. If it finds any,
`revise_plan` produces one revised plan, which passes `apply_plan` again; the points are kept
in `TailoredCV.critique`. No issues: no revision call.

Each tailored Word export also saves its structured draft under `data/tailored_cvs/`. The job
card's **Cover letter / review CV** panel loads these drafts after a restart. Users can edit
the headline, summary and visible bullet text; `tailor.revise_tailored` checks edits against
the source CV's fabrication guards and saves a new Word version. A cover letter can explicitly
cite a selected saved draft; batch document preparation links each letter to the draft it just
created. The centre panel's **Documents** tab lists saved drafts even when their job cards are
no longer in the current search; each draft retains the posting text needed to write a letter.
Older Word CVs already in the CV library can be attached to a job from the same panel. The
quality model extracts a structured CV from the Word file; the user must review and save the
extracted text before it can be linked to a cover letter. The saved draft and its source remain
available after a restart. If the original job card is gone, the Documents tab also accepts
the Word CV together with the job title, company and description, then keeps that posting
with the draft for later letters.

Run end to end in code:
```python
from pathlib import Path
from src.core.llm_provider import get_llm_provider
from src.cv import master_cv_manager as mgr, tailor

tailored = tailor.tailor(mgr.load(Path("data/master_cv.json")), jd_text, get_llm_provider())
Path("output/tailored_acme.json").write_text(tailor.dump_tailored(tailored))
```
`tailored.keyword_coverage` is the structured CV's hit-rate (0–1) over JD hard skills +
must-haves. The saved Word draft also carries the original Word CV's coverage for the same
keywords, so the UI can compare like with like; neither value changes the job match score.

## 3b. Cover letters

`cover_letter.write_letter(master, jd, jd_text, llm, motivation) -> CoverLetter`
(`cv_service.write_cover_letter`, `POST /api/jobs/{job_id}/cover-letter`, agent tool
`write_cover_letter`). The letter is written from the CV the user approved: the reviewed
tailored CV chosen for the job (the newest by default), otherwise the CV selected in the CV
library (not necessarily the Master CV). The LLM fills a fixed `LetterSections` template, so
every letter has four paragraphs: an opening (interest naming the job title + overall fit),
exactly two evidence paragraphs (one CV example each, tied to the role) and a short
conclusion, under 300 body words. The model may use the job's terms when the cited CV entries
support them. `apply_letter` blocks fabrication: each paragraph cites existing CV ids in
`source_ids`, personal claims need a source, named skills must appear in cited CV evidence,
and every number is in its cited sources or in the job description (facts about the employer
may be quoted). Hyperbole is rejected. A rejected
section or broken template triggers one revised draft with specific feedback.
The user reviews and can edit the resulting letter in the app; omitted examples do not block
creation.
Motivation comes **only** from the user's career intent (direction, energising work, target
areas); without one, interest stays grounded in the work described in the posting.
Every paragraph is recorded in `CoverLetter.changes`; rejected ones are reported. The Word
file starts with the greeting, without the CV contact header or date. New CV Word files live in
`output/cvs/`; letters live in `output/cover_letters/`, with editable letter records in
`data/cover_letters/`. The Cover letters tab lists current and earlier letters separately from
the CV library, displays their text, permits edits, exports Word or plain text, and can delete
one letter or all letters with their saved exports. Older generated Word files in the root of
`output/` are moved into those folders when the
document libraries load; older download links continue to work.
Uploaded CV files are stored in `data/cvs/` on upload, with an identical copy in `output/cvs/`.
They are one CV: the library lists it once, the two files are kept identical (the one changed
last, in Word or by the assistant's `edit_cv`, is copied over the other), its id never changes
(`data/cv_ids.json`), and deleting the CV removes both. Opening a library CV only views it (`GET /api/cv/preview/{id}`: PDFs and text as
stored; `.docx` files as a PDF that Microsoft Word renders from a copy in its sandbox, cached in
`data/cvs/.preview/`) or opens its copy in `output/cvs/` in a desktop app (`POST /api/cv/open/{id}`, creating the copy
if needed and reusing an existing same-name copy so earlier edits are kept; `?app=word` opens a
PDF in Word as an editable copy). Saves land in `output/cvs/` and are copied back to the upload. Viewing never parses the CV.

---

## 4. Export to Word

By default a CV is never re-rendered: tailored and general CVs are copies of its Word design
(section 3). The templates below apply only when the user picks one, or when no Word version
of the CV exists.

```bash
python .agent/skills/cv_writer/docx_templates.py --list
python .agent/skills/cv_writer/docx_templates.py \
    --cv output/tailored_acme.json --template classic --out output/cvs/Alex_Example_Acme.docx
```
Accepts either a `MasterCV` or a `TailoredCV` JSON. In code: `docx_exporter.export_docx(cv, path, template)`.

Templates (`classic`, `modern`, `compact`) are `DocxTemplate` objects in
`src/cv/docx_exporter.py`: font, sizes, accent colour, margins, section order. To add one,
add an entry to `TEMPLATES` there. Low-level python-docx helpers are in `src/tools/docx_tools.py`.

**ATS constraints (all templates must keep them):** single column; no tables, text boxes,
images or content in headers/footers; native Word bullet lists ("List Bullet" style);
standard section names (Summary, Experience, Skills, Education, Certifications); dates as
`Mon YYYY – Mon YYYY`.

Name generated CV files `<First>_<Last>_<Company>.docx` under `output/cvs/` (git-ignored).

**ATS read-back** (`ats.check_docx(path, cv, keywords) -> ATSReport`, run after every
tailored export and returned as `TailoredCV.ats`): reads the file's text as a parser would,
and reports contact details not readable as plain text, keyword coverage of the exported
text (case-insensitive), an estimated page count (500 words a page; reported, never a warning:
the user prefers a full-length CV), tables, and contact details
placed in the page header.
