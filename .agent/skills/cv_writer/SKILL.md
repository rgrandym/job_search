---
name: cv_writer
description: Create or update the structured Master CV (including evidence from documents the user supplies, accepted one by one), tailor it to a job description (requirement-to-evidence map, positioning for adjacent, more junior or academic roles, STAR-format bullets, no fabricated facts; revised once only when code finds a reason; a before/after fit check by an independent grader; several versions per job compared side by side against the original), write guarded one-page cover letters, and export them to styled Word (.docx) documents with an ATS read-back. Use when the user wants to import/edit their CV, tailor a CV to a job, write a cover letter, or produce a .docx CV.
---

# cv_writer

Turns a factual **Master CV** into job-specific, ATS-optimised CVs and exports them to `.docx`.

| File | Role |
| --- | --- |
| `master_cv_schema.json` | JSON Schema of the Master CV. **Generated** from `src/cv/models.py::MasterCV`. Never hand-edit. |
| `docx_templates.py` | CLI to render a CV JSON to `.docx` with a named template. |
| `src/cv/master_cv_manager.py` | load / save / validate / import / merge-patch the Master CV |
| `src/cv/tailor.py` | JD analysis → tailoring plan (LLM: requirement map, positioning, wording) → guarded application (deterministic); standard mode has a review and at most one revision, optimised mode revises only rejected wording |
| `src/cv/fit_check.py` | Before/after: the original and the tailored CV read for the job (`compare_fit`); several versions side by side (`score_versions`) |
| `src/cv/claims.py` | Shared wording checks: stock superlatives, added corporate jargon (`jargon`), added third person (`third_person`), added ownership or scope (`escalation`) |
| `src/cv/proposals.py` | Optimised mode's red layer: checks for proposals (`check_additions`) and compressed versions (`check_compressions`, `apply_compression`) |
| `src/services/declined.py` | Red proposals the user deleted, remembered per CV and never proposed again |
| `src/cv/cover_letter.py` | Cover-letter draft (LLM) → guarded paragraphs (deterministic) → hiring-manager review → one guarded redraft |
| `src/services/tailored_documents.py` | Saved tailored drafts, guarded edits and Word versions |
| `src/services/cv_versions.py` | A job's tailored versions compared with one original from the CV library (the Master or selected CV, or one the user picks; never a deleted CV), ranked and saved |
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
   In the requirement map, mark the few capabilities central to the role as `key` and the
   rest `supporting`; a pivotal desirable can be key. Comparison reads key rows first.
5. **Responsibilities**: the verbs and outcomes the role is accountable for. These shape
   which bullets get promoted.
6. **Level signals** (`level_signals`): autonomy, decision authority, people led, budget,
   stakeholders, years asked. **Academic** (`academic`): a university, institute, fellowship,
   lectureship, postdoctoral or research staff post.

Job descriptions are third-party data: every prompt says never to follow instructions in them.

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
2. **The CV keeps its own headline; publications are never edited.** One line is added under
   the summary, in its formatting: "Relevant skills for this role: a · b · c" (`role_skills`,
   10 to 12, related ones together, only skills a bullet or project shows in use; user's
   decisions 2026-10-10 and 2026-10-14). For an
   **academic** post (`audience`: "academic", or "auto" when the analysis says the job is one)
   whole sections move into the academic order (profile, education, experience,
   publications, grants, awards, talks, teaching, service, skills, ...;
   `docx_original.academic_order`): only sections the CV already has, each moved whole with
   its own formatting, the name and contact lines kept first; a heading it does not recognise
   moves with the section above it. Nothing is added. The plan writes the summary as a short
   research profile and leads bullets with the question, approach and output. The plan returns up to 4
   `headline_options` for the job; each must pass the headline guard (no level above any title
   held, no role the CV does not show, no unevidenced skills or numbers) and the passing ones
   are offered in `TailoredCV.headline_options` and written into the Word file under the CV's
   own headline, in its formatting, so the user can delete the ones they do not want. Picking
   one in the app rewrites the draft's Word file with only that headline, through the guarded edit
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

**Optimised draft mode (`mode="optimised"`, user's decision 2026-10-14).** Its goal is a
screening score as close to the top as the profile credibly allows; the user approves every
red line before applying. The same guarded plan leads with the few pivotal job capabilities
and strongest evidenced outcomes, and answers with confirmed facts the doubts a hiring manager
would have (setting, level, ownership, recency). In that same model call, `review_candidates`
proposes what the candidate very probably did but the CV does not say (experienced
professionals routinely leave out techniques, documents, standards and partners), ordered by
how much it would change the interview decision. Inference may draw on a role's bullets,
title, employer and sector, its length, the education, the stored profile and the user's
other parsed CVs (`<other_cvs>`, `cv_service.library_background`; never parsed for this).
Kinds, each placed where a reader expects it (`proposals.check_additions`):
- `rewording` (the main way to bring the job's terms in): an existing bullet rewritten in the
  posting's terms for the same work, in red on the line below it ("[CONFIRM REWORDED LINE
  BEFORE USE: replaces the line above]"); it keeps every number of the line. A factual rewrite
  rejected for a job skill or term the line does not name, or for more ownership or reach,
  becomes one (`proposals.rewordings`); one that adds or drops numbers never does. Confirming
  replaces the line in that draft only; the source CV keeps its own wording.
- `enrichment`: words added inside an existing bullet (`anchor_id`), in red at its end
  (" [CONFIRM: using qPCR and flow cytometry]"); the background quote is from that bullet.
- `experience`: a new bullet, in red after the bullet it relates to (`anchor_id`), in its role;
  the quote is from that role (bullets, title, employer) or the other CVs.
- `metric`: a question for a number a key bullet lacks (" [CONFIRM: how many assays?]");
  confirming needs the line with its real number; never a figure from the model.
- `skill`: in red at the end of its own skills category line (`skill_group`), else under the
  profile.
- `summary` (at most one sentence) and `headline` (at most one; never above the level held).
As many proposals as the job's open gaps and missing terms need (12 to 30,
`AdditionContext.limit`), at most 8 per role, at most 5 number questions. Each states its
`basis` (what people with that title, employer and sector routinely do) and `likelihood`:
`likely` ones come first and `possible` ones are at most a third; nothing such a role would
be unlikely to involve (creative but realistic, user's decision 2026-10-15). Code checks both quotes,
the anchor, that no number the source lacks and no jargon is added, and drops anything the
user deleted before for this CV (`services/declined.py`; similar wording counts as the same).
Wording uses the posting's terms where accurate, once, never trading accuracy for a keyword;
each `reason` names the doubt it settles and what the user must check.
Accurate but fast (user's decision 2026-10-15): when code finds rejected wording, open key or
essential requirements or missing job keywords that no proposal addresses, or a screening
score under grade A (`TARGET_ATS` = 80, with every proposal confirmed), one revision
(`_revised_once`) addresses them all; code scores both drafts and keeps the better one; the
independent grader reads the draft once, after. Proposals the reader credits with nothing (and that
name no missing job term) are dropped once the reader credited some proposal; the rest are
ranked by what they gain (`rank_additions`). Each request saves one Word version.
Proposals remain separate from `TailoredCV.cv` and print in red with `[CONFIRM ...]`.
The user reviews each in the app: delete (optionally with a reason, remembered), or edit and
explicitly confirm. Confirmation adds the corrected fact to the selected source CV (added
words and numbers reword that bullet there; a summary sentence or headline changes this draft
only) and rewrites the draft's Word file; pending text never contributes to the draft's own
score, keyword coverage (the ATS read-back strips red lines and red words inside a line), or
cover letters.

**Compressed versions (optimised mode, user's decision 2026-10-14).** The plan may also
return `compressions`: `tighten` (one long bullet without details this job does not need),
or `merge_bullets` (two or more overlapping bullets of one role as one). Consolidation stays
within one role: different roles are never combined (user's decision 2026-10-15); a
`merge_roles` the model returns is dropped, and one pending in an older draft cannot be applied.
Code (`proposals.check_compressions`) keeps one only if every number and job term of its
sources stays, nothing they do not show is added (numbers, skills, ownership, jargon) and a
tightened line is shorter. Every role keeps its heading lines (title, employer, dates), even
when the title and dates sit above the employer's line (`docx_original._spans`). A compression is
written in red below the lines it would replace, which stay as they are: the app never
removes them. The user compares and chooses: "Use the shorter version"
(`tailored_documents.review_compression`, apply) replaces them in that draft only (a merged
entry in the Word file in its own formatting, the roles' entries removed); "Keep my lines"
removes the red version. The source CV is never changed. The fit
check (and the version comparison) reads the draft again with every pending proposal in place,
in the same blind pass, and reports it apart as hypothetical (`FitComparison.if_confirmed`,
`VersionReading.if_confirmed`: `fit_check.with_proposals`, `project`, `with_keywords`): the
score and requirements with the proposals, each newly shown requirement credited to the
proposal the reader quoted, and the job terms only the proposals name. The standard mode retains
the existing small red skills line.

`tailor.propose_plan(..., steer) -> TailoringPlan`, then `tailor.apply_plan(...)` enforces
the rules. `Steering` carries the job_matcher's verdict on this job (fit summary, reasons,
transferable evidence, gaps), the stored **profile** (`profile_memory.profile_brief`: pitch,
what sets the level, transferable strengths, publication signals and the job's role family
with its positioning and gap), the user's emphasis and level choices, the trim decision,
the **level note** and the academic flag, so the plan leads with what the matcher found and
never papers over its gaps.

The plan works in order:
1. **Requirement map** (`requirements`): every meaningful requirement (essential or
   desirable) with the CV ids that show it and a class: `direct`, `transferable` (equivalent
   work elsewhere), `adjacent` (related, not equivalent), `needs_confirmation` (plausible but
   not in the CV: never claimed, shown to the user as "confirm?") or `not_evidenced` (a gap).
   `check_requirements` drops unknown ids, turns a claimed row with no evidence into a gap,
   and makes a "direct" row whose evidence never names the job's skill only "adjacent" (Azure
   work does not show AWS).
2. **Positioning** (`positioning`): what the candidate can deliver at what level, 2-4
   differentiators, what carries over to a role outside their function, and their level
   against the job's. Dropped if it adds numbers the CV lacks or uses superlatives.
3. **Wording**, starting from the CV's achievements, not the job's language.
   Keep any job-relevant skill named only in the source summary; when the rewrite omits it,
   code promotes the source term to the confirmed relevant-skills line so the new summary can
   still be used.
   The writer may suggest up to five techniques missing from the CV when its documented
   background makes them plausible for quoted posting work. Each suggestion quotes real CV words, appears
   in a separate red "Skills to confirm before use (not yet claimed)" line in Word, and is
   shown in the report. It is not added to the factual CV, fit score or letter evidence.

**Level** (`tailor.level_note`, code): a job below the highest level held gets "lead with
hands-on delivery and the skills used day to day; keep senior experience as supporting
strength further down; never imply less experience". A job above it is a stretch: "lead with
the scope closest to that level; never claim the level". The plan never removes senior
experience for this (only the length rule can leave lines out).

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
- Claiming more ownership or reach than the source (`claims.escalation`): "led", "managed",
  "owned", "drove", "supervised" need an ownership or authority word in the source
  ("led", "responsible for", "management", ...); "global", "international", "strategic",
  "company-wide" need the same word (or stem) in the source. Mentoring is not managing.
- Using the job's own wording for a requirement (soft skills, short must-haves that are not
  hard skills) in a bullet the requirement map does not list as evidence for it
  (`_wording_reason`). This keeps reframing for an adjacent function tied to the line.
- Adding corporate jargon the source does not use (`claims.jargon`: "passionate",
  "results-driven", "leveraged", "utilised", "robust", "seamless", "fostering", "pivotal",
  "synergy", ...). Plain, current wording: one sentence of about 30 words where the source
  allows, no trailing "..., ensuring X" clauses.
- Writing about the candidate in the third person (`claims.third_person`, user's decision
  2026-10-14): the CV speaks as the user, with bullets in the implied first person and the summary
  with "I" (or without a pronoun when the CV's own summary has none). "The candidate", "she",
  "his" or the user's name, when the source line does not use them, are rejected.
- Bringing one job term into more than two bullets (`_limit_repetition`, user's decision
  2026-10-14): screening software only checks that a term is there, and a person reads
  repetition as stuffing. The rewrites beyond the two most relevant lines are undone and
  recorded.

Rejected rewrites keep the original bullet. Every decision is logged in `TailoredCV.changes`
(`accepted`, `reason`). **Report rejected rewrites and `missing_keywords` to the user.** A
missing keyword is a gap to discuss, never something to paper over. If the user confirms they
do have that experience, add it to the **Master CV** first, then re-tailor.

Also produced, and guarded the same way (recorded in `changes` as `summary`):
- Headline suggestions (rule 2), and a user's own headline edit to a saved draft, are rejected
  if they claim a seniority above any title held, a role the CV does not show (role words of the
  title part, before a comma, "|" or dash), or skills or numbers the CV lacks.
- `summary` (two or three short sentences, at most 60 words; user's decision 2026-10-15): who
  the candidate is for this job
  with their years of experience (computed by code from the role dates,
  `tailor.career_years`: the only number a summary or headline may add), the strongest
  relevant achievement from a bullet, and what they bring to this team (distinctive relevant
  breadth: leadership, hands-on practice, adjacent experience). Other numbers and skills must
  exist somewhere in the CV. A summary with several paragraphs keeps their number. The tailoring request
  may emphasise leadership or hands-on work and use a more senior or junior presentation;
  these choices never change the candidate's evidenced level or permit new claims.
- `key_strengths` (user's decision 2026-10-15): 3 or 4 lines of at most 25 words under the
  summary, each citing the CV bullets it states (`source_ids`); checked by
  `tailor._key_strengths` with the same guards as a rewrite of those bullets taken together
  (numbers and skills from them only, no jargon, ownership or third person). Printed as
  "• ..." lines before the relevant-skills line; they count for the screening score.
- `bullet_order`: a list of `{experience_id, bullet_ids}` for every role (a list, because
  strict output schemas such as Codex's cannot carry dict keys) (most relevant first; unlisted bullets follow, or are left out only under
  rule 4) and `skills_priority` (re-orders existing skills only). If trimming left out the only
  bullet carrying a JD keyword, the bullet is put back (`restored_keywords`);
  `missing_keywords` are then true gaps. Keywords are `tailor.ats_keywords`: hard skills and
  must-haves of at most 4 words; requirement sentences are left to the job_matcher's verdict.

**Job terms for the CV's own wording** (`cv/terms.py`, user's decision 2026-10-13). ATS
software matches the job's keywords word for word. The plan's `term_bridges` pair a job
keyword with the exact words of one bullet that show the same thing ("Mammalian cell culture"
← "cultured HEK293 and CHO lines"). Code keeps a pairing only when the term is an ATS keyword
and a general method, domain or practice (never a named tool, acronym, a term with a number,
or a regulated, credential or management term: those need the CV's own word), the quoted
words are in that bullet, and the requirement map cites that bullet for a requirement naming
the term as `direct` (standard) or `direct`/`transferable` (optimised). A kept pairing is that
bullet's evidence for every guard (`with_bridges` adds it as an unprinted skill tag), so the
bullet's rewrite, the summary and the relevant-skills line may use the term; nothing else
changes. The relevant-skills line leads with the job's keywords: the plan's keyword skills, a
paired term no printed line names, then keywords the CV names only further down
(`_job_terms_on_top`). Pairings are listed in the report for the user to check.

**Length advice** (standard mode, user's decision 2026-10-14): `tailor.length_notes` lists
bullets over 35 words, bullets of one role that describe overlapping work, and roles that
ended over 10 years ago with more than 4 bullets (`TailoredCV.length_notes`, shown under the
job). Advice only: nothing is changed; the user edits the lines if they agree.

**Score-driven revision** (`tailor(..., check=...)`, both modes; `cv_service` passes the
check). The draft is scored (screening score, code); under grade A, `reader_feedback` lists
what the score found missing (job keywords not printed, in standard mode only while fewer
than 80% are, `KEYWORDS_ENOUGH`: the right keywords, not more; the job title; the key
requirements the CV evidences) and `revise_for_reader` makes one revised plan, applied under
the same guards and scored again; the better-scoring draft is kept and `TailoredCV.revision`
says what happened. The model's reading (`check`) runs once, at the end.

**Review and revision** (`tailor(..., review=True)`, the default): a second, fresh reader
(`critique_cv`, `CVCritique`), as hiring manager and recruiter, lists JD requirements a Master
CV bullet evidences but the tailored CV leaves out or buries (citing that bullet; points
citing unknown ids are dropped), weak bullets (passive, generic, result buried), up to 3
order notes and up to 3 recruiter notes (does the top say why this candidate fits; do the
differentiators show; is the case for another function or level credible). If it finds any,
`revise_plan` produces one revised plan, which passes `apply_plan` again; the points are kept
in `TailoredCV.critique`. No issues: no revision call.
This separate review applies to standard mode. In optimised mode, code checks the first plan
and asks for one correction only if wording was rejected; no extra model critique runs.

Each tailored Word export also saves its structured draft under `data/tailored_cvs/`. The job
card's **Cover letter / review CV** panel loads these drafts after a restart. Users can edit
the headline, summary and visible bullet text; `tailor.revise_tailored` checks edits against
the source CV's fabrication guards and rewrites the draft's own Word file in place, so a draft stays one file however often it is edited (user's decision 2026-10-13); a file changed in Word since the app wrote it is never overwritten: the edit goes to a new file and a note says so. A cover letter can explicitly
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

## 3b. Before/after fit check (runs on every tailored CV)

**Screening score** (`TailoredCV.screening`, `cv/ats_score.py`, `tailor.screen`; user's
decision 2026-10-14). Deterministic: code scores the printed text, so the same CV and job
always give the same number; no model is involved. Built the way ATS match rates are
(employers' own systems are not public): job keywords in the posting's own words
(`tailor.literal_terms`), each weighted by how often the posting uses it (1 to 3): 65 points;
the job title (before any comma or dash) in the CV: 15 (the share of its words in the
headline and role titles at half value otherwise); degrees the posting names: 10; its soft
skills: 10. A part the posting does not ask for is left out and the rest scaled to 100.
Terms match as an ATS search does (`ats_score.Text.has`): case, plural or singular, hyphens
and slashes ignored ("post-doctoral" = "postdoctoral"). When the posting writes a term with
its abbreviation ("liquid chromatography-mass spectrometry (LC-MS)"), either form in the CV
counts (`cv/acronyms.py`; initials must match). Grades: A 80+, B 65+, C 50+, D.

**Both forms of a term** (user's decision 2026-10-15; `tailor._spell_out`, code only): an ATS
search is literal, so a job keyword the tailored CV lacks while a bullet shows its other form
is written there once as "long form (ABBR)" ("natural language processing (NLP)"). Pairs come
from the posting or the CV (bracketed, initials checked) or the known skill synonyms; an
abbreviation matches only in its own capitals ("10 ml" is never "ML"). The same term, so no
new fact; the change is recorded in `TailoredCV.changes`.
It is scored for the original, the tailored CV and (optimised) the CV with every red
proposal confirmed, with the keywords found and still missing; it is shown first and is what
tailoring aims at. It is scored again when a proposal is confirmed or a compressed version
applied. `FitComparison` (the model's reading: requirements shown and the search-anchored fit)
is shown second as the AI screen. A tailoring's reading is one call (`TAILOR_READS` = 1),
made once at the end; the original is read once per job and kept with its requirements
(`OriginalReading`, `data/fit_baselines.json`, `cv_service.load_baseline`).

`fit_check.compare_fit(source, tailored, jd, jd_text, llm) -> FitComparison`, run by
`cv_service.tailor_to_job` after the revision (user's decision 2026-10-10: always on). Code
measures the job's ATS keywords in the whole CV and in its first ~450 words, for the original
(the CV as aligned to its Word file) and the tailored CV (as printed, with the role-skills
line). One LLM call reads both CVs as a recruiter, labelled CV 1 and CV 2 in an order fixed by
a hash of the job and the texts (so it cannot learn which is tailored), rating function,
domain, seniority, leadership and sector 0-4 from the page alone and listing which of the
job's essential requirements (the core requirements the search found unmet first, then the
requirement map, else must-haves; at most 15) each clearly shows. It is the same checked
reader as the version comparison: every rating above 1 and every requirement must quote a
line code finds in that CV. When the search verdict is available, its score is the original baseline and the
reader's before/after level changes adjust that score using the job matcher's exact weighted
point conversion (30/20/20/10/10); practicality (10) is held fixed. Both sides go through the
matcher's own capped total (`fit_rubric.capped_score`: unmet core requirement 55, unread posting
62, a weak dimension 65), and the score moves by the difference, so a capped search score moves
only as far as the matcher would move it. The unmet-core cap (55) came from what the CV
showed, so it lifts for a CV that newly shows every core requirement the search found unmet,
each with a quoted line, when the original CV's reading did not (`fit_check._still_unmet`);
this applies to the draft, to an optimised draft's "if confirmed" projection and to the
version comparison. The verdict's unmet core requirements are also passed to the tailoring
plan (`matcher_guidance`), and an optimised plan addresses each first: source evidence where
the CV has it, else a review candidate. Without a saved verdict, practicality is neutral (level 2) and both CVs are scored
from the same rubric. `worse` lists any
dimension or requirement count that dropped. The candidate's actual fit is unchanged (same
facts); the job_matcher's verdict judges that. A failed check never loses the CV: it is noted
in `document_notes`.

## 3b2. Comparing a job's CV versions

Every tailoring is kept as its own version for the job, with the choices it was written with
(`TailoredDocument.settings`: design, emphasis, level, length, audience) and a label numbered
per job (`cv_versions.labels`: "V1 · Automatic", "V2 · Level: more senior"). The job card
lists the versions and shows the chosen one's report.
`cv_versions.compare(ws, job_id, ids, baseline_id)` (`POST /api/jobs/{job_id}/cv-comparison`,
agent tool `compare_cv_versions`) reads the chosen versions and **one** original from the CV
library as it is now (the Master CV, else the selected CV, unless the user picks another; a
deleted CV is refused), every version set against it, side by side in one LLM call (`fit_check.score_versions`, at most `MAX_VERSIONS` = 6 CVs), in an order
fixed by a hash of the job and each text, so no CV is identified and one reader applies the
same standard to all. The original's text is the oldest chosen draft's source CV as tailoring
read it, without red suggestions confirmed since (`tailored_documents.as_tailored`): the CV
the search scored, so a confirmed fact shows as an improvement of the version holding it, and
confirming keeps the anchor. The saved search score is reused for the original when every
version comes from that CV and the verdict judged it (`JobVerdict.cv_id`; verdicts from before
it was recorded are taken as they are); each version's changed dimension ratings adjust that
score with the search weights while practicality stays fixed. Older drafts may use the retained
job verdict. Otherwise every CV uses neutral practicality and `search_fit_note` says why. Same
rubric, requirement list (the newest version's) and ATS
keywords as 3b. Code then sets each version against its original (`version_areas`: every
dimension, requirements shown and each requirement gained or lost, keyword coverage, keywords
on the first page, keywords added or lost: better / worse / same) and picks the best
(`best_version`: checked requirements and scores decide; an equal reading has no winner). The
comparison is saved per job (`data/cv_comparisons/`) and marked stale once a version it read
is edited or removed, or its original leaves the library.

**Deterministic and checkable (user's decision 2026-10-11).** Each comparison is read
`READS` = 3 times in parallel, each in another CV order (`fit_check.read_versions`). Every
rating and every requirement met must quote a line of that CV (`EvidencedReading`); code checks
each quote (`fit_check.verify`: case, spacing and punctuation ignored, "..." pieces, at least two
words): a requirement without a found quote does not count, and a rating without one is capped
at `UNQUOTED_CAP` = 1. The median rating per dimension and the requirements most reads found
count (`consensus`). Each CV's reading is remembered (`data/cv_comparisons/readings.json`, keyed
by `fit_check.reading_key`: CV text, job, requirements, rubric, reads and the CV model), so
comparing again gives the same scores; only CVs not read before are read, still beside the
others. Code then answers the questions with fixed margins: each version's verdict against
its original (`fit_check.verdict`: better / worse / mixed / same; the score by `NOISE_MARGIN`
= 5 or any quoted requirement gained or lost; exact keyword changes are shown as diagnostic
detail, not proof of overall fit), what
a setting did (`cv_versions.setting_effects`: versions that differ in exactly one of level,
length, emphasis, audience; "clear" or "no effect" by the same margins), and the best version
(`best_version`: scores within the margin of the top count as the same; among those the checked
requirements and score decide before the exact job keywords; the summary names
what decided and what the runner-up does better). Each version also reports how many lines tailoring reworded (`rewritten`). Like 3b it measures how clearly each CV shows the fit, never the
candidate's actual fit.

## 3c. Cover letters

`cover_letter.write_letter(master, jd, jd_text, llm, motivation) -> CoverLetter`
(`cv_service.write_cover_letter`, `POST /api/jobs/{job_id}/cover-letter`, agent tool
`write_cover_letter`). The letter is written from the CV the user approved: the reviewed
tailored CV chosen for the job (the newest by default), otherwise the CV selected in the CV
library (not necessarily the Master CV). When written from a tailored CV, the letter follows
its **positioning and requirement map** (`letter_context`) and never claims a requirement it
marks as a gap. The letter travels with the CV, so it does not repeat it: no contact header,
date or recap (user's decision 2026-10-10). The LLM fills the **master template** (user's
decision 2026-10-11, `LetterSections`; the model adapts the wording, never the structure):
1. **Introduction**: "I am applying for the <exact title> role at <organisation> because
<reason from the career intent, else the posting's work>" + "My experience in <two to four CV
areas> fits the requirements and responsibilities of this role" (cited).
2. **Evidence**: one or two short paragraphs, each on one of the job's most important
requirements: one CV example, its result, and why it matters for the role.
3. **Gap** (`bridge`): required when code finds essential requirements the tailored CV's map
marks `not_evidenced` or `needs_confirmation` (`letter_gaps`, sent as `<gaps>`), or the role
is a change of function or level: "I bring extensive experience in <cited strengths>.
Although I have not yet worked directly in <the gap, named plainly>, I learn quickly and can
build what is missing, as the range of <cited skills> I have mastered over the years shows."
The paragraph must cite the CV entries behind the strengths and that range; a gap is never
presented as experience; motivation for a move only from the career intent.
4. **Conclusion**: the contribution the candidate could make, inviting a conversation.
The body stays under `MAX_LETTER_WORDS` = 350 words so it fits one page. The model
may use the job's terms when the cited CV entries support them. `apply_letter` checks for fabrication: each paragraph cites existing CV ids in
`source_ids`, personal claims need a source, named skills must appear in cited CV evidence,
and every number is in its cited sources or in the job description (facts about the employer
may be quoted). Hyperbole, clichés ("perfect fit", "hit the ground running") and added
ownership or scope over the cited entries (`claims.escalation`) are caught. A failed check
or broken template triggers one revised draft with specific feedback; the draft with fewer
problems is kept. **The letter is always written (user's decision 2026-10-11):** a paragraph a
check still questions is kept and flagged (`CoverLetter.flags`, red in the Word file and in
the app, with the reason), and template issues (role not named, section missing, too long)
are listed as `notes`; the user edits the paragraph or keeps it as written (`LetterEdits.keep`;
an unchanged kept paragraph is not flagged again). The letter is then read by the hiring
manager (`review_letter`, `LetterReview`: at most 4 fixes: generic opening, recited CV, top
requirements not addressed, an unconvincing or not honest case for the move, too long); with
fixes, one redraft is made and used only if the checks find no more problems in it. A
one-letter skill (R, C) counts only as a standalone capital letter, never inside "R&D" or
"U.S." (`search_tools.mentions`).
The user reviews and can edit the resulting letter in the app; omitted examples do not block
creation. Saving an edit rewrites the letter's own Word file (one file per letter, user's
decision 2026-10-13); a file changed in Word since the app wrote it, or an older letter the
app did not create, is never overwritten: the edit goes to a new file.
Motivation comes **only** from the user's career intent (direction, energising work, target
areas); without one, interest stays grounded in the work described in the posting.
Every paragraph is recorded in `CoverLetter.changes`; flagged ones are shown in red. The Word
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

**Independent grader** (user's decision 2026-10-15): the before/after reading and version
comparisons use the screening model (`cv_service.GRADER`), never the CV writer, so a draft is
not graded by the model that wrote it; a kept reading of the original from another model is
read again. Its `impression` is what a recruiter takes from a six-second skim of the top.

**Readability** (`cv/readability.py`, code only): bullets of at most 35 words (25), strong
openings (20), active voice (10), varied openings within a role (10), a summary of at most 80
words in four sentences (15), the job's keywords the CV shows within its first 150 words
(20); shown beside the screening score, original → tailored, with what to fix.

**Red changes and the final version** (user's decision 2026-10-15): in a draft every changed
or added line is red (`docx_original._mark`, `docx_exporter` for templates): rewritten
bullets, the summary, key strengths, the relevant-skills line, reordered skills lists and
suggested headlines, besides the proposals. `tailored_documents.finalise` writes
`<draft>_final.docx` beside it: all black, its headline only, nothing pending (what was left
out is listed in `final_notes`); any later edit of the draft clears it.

**Shorter earlier roles** (both modes, `proposals.condensed_roles`, code only): a role that
ended over 10 years ago with more than three lines gets a red `condense_role` proposal
keeping its two most relevant lines (those naming a job term first); `source_ids` are the
lines it leaves out, `removed` their text. Only the user applies it.

**CV health check** (`services/cv_health.py`): the selected CV's Word design read as an ATS
does (`check_docx`), what its facts leave out (`knowledge_gaps`: roles with one line or no
result, skills used but not listed, no summary or education) and its readability;
`clean_template` adds an ATS-safe classic copy with the same facts to the library and selects
it.

**Cover letters**: drafted once and redrafted only when the checks question something (the
app makes no separate review call); `cover_letter.score_letter` scores the result by code:
the job's most used terms named (35, full at half), company and exact role (20), 250 to 400
words (15), sentences averaging 22 words or fewer (15), unquestioned paragraphs (15).

**ATS read-back** (`ats.check_docx(path, cv, keywords) -> ATSReport`, run after every
tailored export and returned as `TailoredCV.ats`): reads the file's text as a parser would,
and reports contact details not readable as plain text, keyword coverage of the exported
text (case-insensitive), an estimated page count (500 words a page; reported, never a warning:
the user prefers a full-length CV), tables, and contact details
placed in the page header. Layout warnings (`ats.layout_warnings`, user's decision 2026-10-14;
warnings only, the design stays the user's): two or more columns (parsers read straight
across), contact details in the footer, text boxes and images (their text is not read), roles
with no year next to them (systems count years of experience from the dates), role dates in a
style parsers may misread (numeric months "01/2020", short ranges "2020–21", seasons, "to
date"; "Jan 2020 – Present" is safe; user's decision 2026-10-15), no standard Experience,
Education or Skills heading, and unusual fonts. The fit score is shown with an A–D grade
and labelled as the app's estimate, not an employer's number.
