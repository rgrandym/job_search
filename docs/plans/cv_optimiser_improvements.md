# Plan: CV tailoring and optimiser improvements

Status: implemented 2026-10-14, awaiting manual testing

Sources:
- Video 1: "I Tested 50 ChatGPT Resume Prompts – These 5 Actually Work" (Heather Austin).
- Video 2: "How to Make an ATS-Friendly Resume in 2026 (Full Walkthrough)" (Jobscan,
  transcript), plus Jobscan's article "What match rate should I aim for".
- The current code: `src/cv/tailor.py`, `src/cv/claims.py`, `src/cv/docx_original.py`,
  `src/cv/fit_check.py`, `src/cv/ats.py`, `src/services/tailored_documents.py`.

## The two modes

| | Standard | Optimised |
|---|---|---|
| Goal | Read clearly better than the original to ATS and AI screens | Bring the match score as close to the top as the profile credibly allows |
| What it writes | Only confirmed CV facts, reworded and reordered | Standard's factual CV **plus** a red layer of inferred experience, skills and numbers for the user to approve |
| Creativity | None beyond rewording | Allowed: infers what someone in each role very probably did but didn't write down |
| Keywords | Right keywords, not more (stop chasing at about 80%) | Aims for full coverage of the job's key terms, through confirmed wording or red proposals |
| Who decides | Code checks; the user picks a version | Code checks the anchors; **the user approves every red line** before applying |

Why optimised matters: experienced professionals routinely leave out skills and work they
really did. Their CV shows the outcome, not every technique, document or partner involved.
The optimiser's job is to surface those omissions for this job, so the user can confirm them.

Limits that stay in **both** modes (CLAUDE.md, No-Fabrication Contract):
- Nothing red counts as fact, or towards the factual fit or ATS score, until confirmed.
- No invented numbers, employers, dates, credentials or team sizes. A missing number becomes
  a question (item O3), never a figure.
- No GMP, regulatory or managerial experience inferred from academic work alone.
- Confirmed wording goes into the source CV; a deleted proposal is never applied.

---

## Part A: both modes

### A1. Jargon and AI-tone check

- Extend `claims.py` with a jargon list. A rewrite, summary or headline that **adds** one of
  these words, when the source doesn't have it, is rejected and the original kept (same
  pattern as `escalation`). The same check applies to red proposals. Starting list:
  passionate, team player, results-driven, dynamic, self-starter, detail-oriented,
  proven track record, leverage(d), synergy, cutting-edge, robust, seamless(ly), utilise,
  fostering, pivotal, meticulous, showcasing, drive impact.
- Plain-style rules in `PLAN_SYSTEM` rule 5 and the proposal prompts: plain verbs; no
  trailing "…, ensuring X" / "…, driving Y" clauses; no em-dash chains; about 30 words or
  fewer per bullet.
- Why: the human reader. Video 2 says AI screeners match and rank; in Jobscan's testing none
  of them detect AI writing. The recruiter's 30-second read is what this protects.
- **Decided (2026-10-14): cover letters too.** There a jargon match flags the paragraph in
  red with its reason (`CoverLetter.flags`), per the cover-letter rule; the letter is never
  refused.

**Your notes:**

### A2. Keyword repetition limit

- Video 2: Jobscan tested repeating a keyword and it gave "zero boost"; a wall of repeated
  buzzwords is a red flag to the human reader.
- Each job term printed about 3 times at most, in at most 2 bullets (red proposals included).
  Over the limit, keep the rewrites with the fewest uses and record why.

**Your notes:**

### A3. Headline matching the job title

- Video 2: the job title at the top is "the highest value place" to match the job's wording
  (Jobscan: more than 10× interview invitations).
- The first suggested headline is the job's exact title whenever the headline checks pass.
- Years of experience: code computes them (`_career_years`) and the headline and summary
  checks allow that one number. Today `_apply_headline` rejects any number not already in
  the CV.

**Your notes:**

### A4. Summary in 3 sentences

Video 1's "who / proof / why":
1. Who the candidate is, with the computed years of experience.
2. The strongest relevant achievement, taken from a bullet.
3. What they bring to this team.

The existing rule for summaries with several paragraphs stays.

**Your notes:**

### A5. Skills line and categories

- Raise `MAX_ROLE_SKILLS` from 8 to 10–12, grouped by theme, using the job's exact words
  through term pairings.
- **Only skills a bullet shows in use** (video 2: "each backed by a real bullet point";
  Jobscan: recruiters are "wary of overly broad skill lists").
- Reorder whole skill groups by relevance to the job, not just the skills inside each group.
  No new groups, nothing renamed (the "same design" rule).

**Your notes:**

### A6. Word read-back warnings (`ats.py`, no model calls)

From video 2's formatting section. All are warnings only: the tailored CV copies the
user's design.
- Two-column layout (the parser reads across and mixes the columns).
- Contact details in the footer (the header is already checked).
- Images and text boxes: text in them isn't read. Name them.
- Role dates not in a standard form ("Mar 2021 – Present", "2021–2024"): systems count years
  from them, and recruiters filter by years.
- Non-standard section headings; unusual fonts (optional).

**Your notes:**

### A7. Score labels (optional)

- Label the app's scores as an estimate of how screens read the CV (as Jobscan does).
- Show an A–D grade beside the fit score (Workday HiredScore grades applicants A–D).

**Your notes:**

---

## Part B: standard mode only

### S1. Right keywords, not more

- The score-driven revision (`reader_feedback`) currently lists **every** missing job keyword
  for the model to add. Instead, list only terms a bullet can show (a possible term
  pairing), and once keyword coverage is about 80% (Jobscan's article), aim the revision at
  requirements not shown instead.
- Standard mode doesn't add red proposals except the existing optional "skills to confirm"
  line.

**Your notes:**

### S2. Length advice

**Decided (2026-10-14): advice only.** Standard mode keeps every line ("Nothing is removed"
is unchanged for standard mode). A short "Length notes" list in the app (repeated work that
could be merged, bullets over about 35 words, long older roles), applied by the user with
`edit_cv`. Rough targets: summary 50–70 words, skills line 10–12, bullets 1–2 lines.

**Your notes:**

---

## Part C: optimised mode only

Today: the plan proposes up to 8 `review_candidates`; code checks their anchors
(`_review_additions`); a second hiring-manager pass runs **only when none survive**. Skill
proposals go under the profile, experience proposals at the end of their role.
`ProjectedFit` reads the draft with all proposals in place but doesn't steer anything.

### O1. Aim at a target score, not "some proposals"

- Use `ProjectedFit` as the optimiser's target. If the projected score is below a target
  (e.g. 85, or the top of the scale less what the profile can't credibly cover), run one
  more proposal pass aimed at what is still not shown: key or essential requirements and
  job key terms.
- Run that pass whenever key gaps remain open, not only when no proposal survived.
- Raise the cap from 8 to cover every open key gap (e.g. up to 12), still at most 2–3 per
  role.
- Drop proposals behind no requirement or term gained (`ProjectedFit.no_effect`); order the
  rest by gain.
- Show the user: "Confirmed: 72 · If you confirm all: 91", and per proposal what it adds.

**Your notes:**

### O2. A red layer across every section

Proposals in the section where a reader expects them:
- **Inline enrichments of existing bullets (new, the main one for experienced CVs):** the
  confirmed bullet stays as it is, with a red insert naming the technique, partner or
  standard the work very probably involved, e.g. "Validated cell-based potency assays
  **[CONFIRM: using qPCR and flow cytometry, to ICH Q2]**". Anchored on that bullet.
- **New experience bullets:** after the bullet they come from, not at the end of the role.
- **Skills:** in the matching skills category, in red (fallback: the line under the
  profile).
- **Summary:** at most one red sentence when a key requirement is only covered by proposals.
- **Headline:** a red headline option when the job title matches the user's function but no
  confirmed evidence supports one of its words.
- **Numbers:** see O3.

Model change: `AdditionCandidate.kind` gains `enrichment`, `summary` and `headline`; an
anchor bullet id and an optional `skill_group`, all checked by code. Run `export-schema`.

**Your notes:**

### O3. Questions about missing numbers

- When a key bullet has no figure but one would normally exist, a red placeholder asks for
  it: "Ran assay validation for **[CONFIRM: how many assays / samples?]**".
- The 3–5 bullets that matter most for the job.
- An answered number goes into the source CV and the draft's Word file is rewritten. An
  unanswered question never becomes a number.

**Your notes:**

### O4. Broader, more creative inference

Today an anchor must be exact words from that role's bullets. Experienced CVs often say
less than the role involved, so also accept, as background for an inference:
- the role's title, employer and its sector (e.g. a Senior Scientist at a CDMO very probably
  wrote batch records and worked to SOPs);
- the role's length and seniority (more years, more likely to have done the routine
  surrounding work);
- education, projects and publications (methods named there);
- the stored profile (`profile_memory`: role families, strengths);
- other CVs in the user's library.

The proposal still quotes its anchor exactly (code checks it) and says what the user must
check. The academic-work limit above still applies. The prompt asks for what "someone in
this role at this employer would very probably have done", not only what the bullets imply.

**Your notes:**

### O5. Full keyword coverage, through the red layer

- Every job key term (`ats_keywords`) not shown by the confirmed CV gets either a term
  pairing or a red proposal that names it, when the profile makes it credible.
- The 80% stop (S1) doesn't apply here; the repetition limit (A2) does.
- `ProjectedFit.keywords_projected` reports the coverage if all are confirmed.

**Your notes:**

### O6. Remember the user's answers across jobs

- Today a deleted proposal only affects that draft. The next job can propose it again.
- Keep a small record of declined inferences (text, role, reason if given). The optimiser
  never proposes them again, and passes them to the prompt as "the user has said they did
  not do this".
- Confirmed proposals already go into the source CV, so they become facts for later jobs.

Files: new `data/` file (git-ignored), `src/services/tailored_documents.review_addition`,
`tailor._review_additions`.

**Your notes:**

### O7. Merging and tightening bullets

**Decided (2026-10-14): yes, in optimised mode, for the user to approve.** The aim is a high
score and a CV that is easier for a human to read. Merges keep the main ideas; elements
that are not particularly relevant to this role may be removed.

- **What it proposes:** overlapping bullets merged into one, and long bullets tightened by
  removing details that don't matter for this job.
- **What must stay:** the main idea of every source bullet, and every fact, number, skill
  or job term that matters for this job (a requirement it evidences, an ATS key term, a
  result). Details that serve none of these may go.
- **No new facts:** the merged text uses only facts from its source bullets (it cites every
  source id; the existing guards check numbers, skills and ownership against all of them
  together).
- **Shown for review (decided 2026-10-14):** the original bullets stay exactly as they
  are. The merged or tightened version is **added below them in red**, labelled for review;
  in the app, the sources and what was removed are listed beside it.
- **Never merged:** publications; education. Bullets from different roles only as part of
  a role merge (below).
- **Numbers (decided 2026-10-14):** a merge or tightening never drops a number, even one
  unrelated to the job. Numbers are the strongest proof for the human reader.
- **No rule change:** the app never removes anything; "Nothing is removed" stays as it is
  in both modes. Only the user removes lines: by deleting them in Word, or with an explicit
  "use the compressed version" choice in the app, which removes the originals and turns the
  red version into normal text. The source CV is never changed: it applies to that draft
  only.

**Merging similar roles (decided 2026-10-14).** Older roles can still hold relevant
experience, e.g. consecutive postdoctoral positions over several years whose work overlaps
and reinforces. Optimised mode may propose combining them into one entry:
- **Every institution is kept**, with its own title and dates, on the entry's heading lines
  (e.g. "Postdoctoral Research Fellow, Institution A (2010–2014); Institution B
  (2014–2019)"). For academic and research roles the institution matters, and separate dates
  let screening software count years (video 2).
- The bullets of the merged roles are consolidated under that entry: overlapping work merged,
  each main idea kept, every number kept, the most relevant first.
- Only roles of the same kind (similar title and function) are proposed for merging; never
  roles at different levels or in different functions.
- No new facts: the consolidated bullets cite all their source bullets, and the guards check
  against all of them.

**How it is shown and decided:**
- **The user decides in the app, per role or role group,** whether to compress it: keep as
  it is / tighten bullets / merge with similar roles. The optimiser proposes; it never
  compresses without that choice.
- **The compressed version is added in red directly below the original entries** (in the
  Word file and the app), which stay untouched, so the user can compare and approve.
  Approving removes the originals and keeps the compressed version in normal text;
  rejecting removes the red version. Either way it is the user's action.
- `ProjectedFit` reads the draft with and without the compression, so the user sees whether
  it costs any requirement or job term.

Model changes: a merge proposal (source role ids and bullet ids, merged heading lines,
consolidated bullets, removed details) checked by code; `export-schema` after the change.
Files: `src/cv/models.py`, `src/cv/tailor.py`, `src/cv/docx_original.py` (writing a merged
entry, in red, after the last of the original entries in the copy of the user's Word CV:
the same red-insertion approach as the existing proposals, so the "same design" rule holds), `src/services/tailored_documents.py` (approve /
reject), the UI, tests.

**Your notes:**

---

## Rules and docs to update

Items A3–A5, O1, O2, O4 and O7 (red compressed versions, added for the user to approve)
change rules in CLAUDE.md (No-Fabrication Contract,
Tailored CV Rules) and `.agent/skills/cv_writer/SKILL.md`, which ship with the code and
tests together.

## Checks for every item

- `pytest`, `ruff check src tests .agent`, `mypy src`; `npm run typecheck && npm run lint`
  in `web/`.
- No-fabrication guards on the factual CV are not weakened.
- Schema changes: `python -m src.cv.master_cv_manager export-schema`, committed with the
  model.
- Before/after: run both modes on a few saved jobs; compare fit, ATS and projected scores
  with the current version.

## Suggested order

1. A1, A2, A6 (small, low-risk).
2. O1, O2, O3 (the optimiser's score gains).
3. O4, O5, O6.
4. A3–A5, S1.
5. S2, O7.

## Your additions

