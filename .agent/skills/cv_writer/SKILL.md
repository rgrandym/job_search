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
| `src/cv/ats.py` | Reads the exported .docx back the way an ATS does |
| `src/cv/docx_exporter.py` | Word rendering (CV and cover letter) + template definitions (`TEMPLATES`) |
| `src/services/enrichment.py` | Evidence review queue: document → proposed CV additions → user accepts |

The Master CV lives at `data/master_cv.json` (git-ignored, personal data). Example:
`data/examples/master_cv.example.json`.

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

## 3. Rewrite bullets as STAR accomplishments, without hallucinating

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
dropping irrelevant detail, promoting relevant bullets, omitting irrelevant ones (keep ≥ 2/role).

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

Also produced, and guarded the same way (recorded in `changes` as `headline` / `summary`):
- `headline`: rejected if it claims a seniority above any title held (or the CV headline), a
  role the CV does not show (role words of the title part, before a comma, "|" or dash), or
  skills or numbers the CV lacks. "Staff ML Engineer" for a Senior is rejected.
- `summary` (2–3 sentences): numbers and skills must exist somewhere in the CV.
- `bullet_order` (keep ≥ 2 per role) and `skills_priority` (re-orders existing skills only).
  If the order left out the only bullet carrying a JD keyword, the bullet is put back
  (`restored_keywords`); `missing_keywords` are then true gaps.

**Review and revision** (`tailor(..., review=True)`, the default): a second, fresh reader
(`critique_cv`, `CVCritique`) lists JD requirements a Master CV bullet evidences but the
tailored CV leaves out or buries (citing that bullet; points citing unknown ids are dropped),
weak bullets (passive, generic, result buried), and up to 3 order notes. If it finds any,
`revise_plan` produces one revised plan, which passes `apply_plan` again; the points are kept
in `TailoredCV.critique`. No issues: no revision call.

Run end to end in code:
```python
from pathlib import Path
from src.core.llm_provider import get_llm_provider
from src.cv import master_cv_manager as mgr, tailor

tailored = tailor.tailor(mgr.load(Path("data/master_cv.json")), jd_text, get_llm_provider())
Path("output/tailored_acme.json").write_text(tailor.dump_tailored(tailored))
```
`tailored.keyword_coverage` is the ATS hit-rate (0–1) over JD hard skills + must-haves.

## 3b. Cover letters

`cover_letter.write_letter(master, jd, jd_text, llm, motivation) -> CoverLetter`
(`cv_service.write_cover_letter`, `POST /api/jobs/{job_id}/cover-letter`, agent tool
`write_cover_letter`). The LLM drafts 3–4 paragraphs, each citing in `source_ids` the Master
CV bullet, role or project ids its claims come from. `apply_letter` keeps a paragraph only if
every cited id exists, every number is in its cited sources or in the job description (facts
about the employer may be quoted), and every skill it names is evidenced by its cited sources.
Motivation comes **only** from the user's career intent (direction, energising work, target
areas); without one, it stays to one brief sentence about the role. Every paragraph is
recorded in `CoverLetter.changes`; rejected ones are reported. Exported as
`<First>_<Last>_<Company>_cover_letter.docx`.

---

## 4. Export to Word

```bash
python .agent/skills/cv_writer/docx_templates.py --list
python .agent/skills/cv_writer/docx_templates.py \
    --cv output/tailored_acme.json --template classic --out output/Alex_Example_Acme.docx
```
Accepts either a `MasterCV` or a `TailoredCV` JSON. In code: `docx_exporter.export_docx(cv, path, template)`.

Templates (`classic`, `modern`, `compact`) are `DocxTemplate` objects in
`src/cv/docx_exporter.py`: font, sizes, accent colour, margins, section order. To add one,
add an entry to `TEMPLATES` there. Low-level python-docx helpers are in `src/tools/docx_tools.py`.

**ATS constraints (all templates must keep them):** single column; no tables, text boxes,
images or content in headers/footers; native Word bullet lists ("List Bullet" style);
standard section names (Summary, Experience, Skills, Education, Certifications); dates as
`Mon YYYY – Mon YYYY`.

Name output files `<First>_<Last>_<Company>.docx` under `output/` (git-ignored).

**ATS read-back** (`ats.check_docx(path, cv, keywords) -> ATSReport`, run after every
tailored export and returned as `TailoredCV.ats`): reads the file's text as a parser would,
and reports contact details not readable as plain text, keyword coverage of the exported
text, an estimated page count (500 words a page; warns above 2), tables, and contact details
placed in the page header.
