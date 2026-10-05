---
name: job_search
description: Capture job postings (LinkedIn public search, Reed, CV-Library, Adzuna, Totaljobs, jobs.ac.uk, NHS Jobs, Biotechnology Jobs, LinkedIn/Indeed alerts and saved postings, company career sites) using title/location/radius/salary filters, pre-filter them on hard constraints, and select the ones that truly match the candidate using a remembered profile summary (with evidence-checked role families and the user's career intent) and the job_matcher's semantic judgement. Use when the user wants to find, import, score or rank jobs.
---

# job_search

Pipeline (`src/services/search_service.py::run_search`, shared by the UI, the API and the agents):

```
filters ─▶ 1 capture ─▶ 1b set aside ─▶ 2 pre-filter ─▶ 3 enrich ─▶ 4 profile summary ─▶ 5 job_matcher ─▶ ranked true matches
           sources      applied / N/A    deterministic    full text    quality model,       semantic verdict
                        (tracker)        (hard rules)     for short-   remembered per       per posting
                                                          list         CV + role family
```

**Matching philosophy.** Keyword overlap is not a match. The deterministic scorer only removes
what is certainly wrong (hard constraints) and orders a shortlist cheaply. The decision about
what *truly* matches is made by the **job_matcher** (the screening model, with second opinions
near the threshold from the quality model), which reads each posting like a
senior recruiter against an evidence-based **profile summary** of the candidate.

| File | Role |
| --- | --- |
| `src/jobs/sources/` | One adapter per source (§1) |
| `src/jobs/scorer.py`, `matcher.py` | Hard exclusions + pre-filter score (§2) |
| `src/jobs/profile_memory.py` | Profile summary (quality model), role families, memory (§4) |
| `src/services/intent.py` | The user's career intent, per CV (§4) |
| `src/services/calibration.py` | Read-only outcome review (§1b) |
| `src/jobs/screener.py` | job_matcher: batch semantic screening (§5) |
| `src/services/model_compare.py` | CLI (`/compare_models`, `model_compare.toml`): re-screen your labelled jobs (or past verdicts, `reference = "history"`) with candidate setups and score them against your labels |
| `job_schema.json` | Generated JSON Schema for `JobPosting` lists |
| `scoring_engine.py` | CLI: deterministic pre-filter report for a CV + jobs file |

---

## 1. Capture (search filters)

`SearchQuery` = the UI form: `titles`, `keywords`, `locations`, `distance_miles`, `salary_min`,
`salary_max`, optional `seniority_min` / `seniority_max`, `work_arrangements`,
`posted_within_days`, `sources`, `exclude_boards`.

**Source picker.** `fetcher.SOURCE_CATALOG` gives every selectable source a label and a
category (`job_boards`, `company`, `alerts`); `GET /api/state` returns it. The sidebar shows the
three categories as switches; clicking one opens its sources, each switchable (a new catalog
entry appears there by itself). The company panel lists every board (`GET /api/companies/boards`,
one row per `CompanyBoard.key`); boards switched off go in `SearchQuery.exclude_boards` and
`CompanyCareersSource` does not read them.

- Sources with server-side search (Reed, CV-Library, Adzuna, LinkedIn, Totaljobs, jobs.ac.uk,
  NHS Jobs) run **one query per title**
  (`SearchQuery.search_terms()`), with location, radius and salary passed to the API. Their
  results are marked `within_search_area` so radius-matched towns aren't mistaken for mismatches.
- Local sources (inbox, demo, Biotechnology Jobs) apply a loose recall filter (`is_relevant`):
  title-family overlap (abbreviations expanded: "Sr. ML Eng" = "senior machine learning
  engineer") or any keyword.

**Date posted** (`posted_within_days`: 1 = last 24 h, 3, 7, 14, 30; null = any time) is
applied in `fetch_all` to every source, and before limits in the local feeds. Adzuna receives
it as `max_days_old`; Workday and SmartRecruiters skip older postings before opening them
(Workday's "Posted 3 Days Ago" label, SmartRecruiters' `releasedDate`). Postings without a date
are kept and counted in the capture message. Alert emails take the email's date (a job is
posted no later than the alert that lists it).

| Source | How | Setup |
| --- | --- | --- |
| **Reed** | Official API. Search returns snippets; full text is fetched lazily for the shortlist (`ReedSource.enrich`). | `JOBSEARCH_REED_API_KEY` |
| **CV-Library** | Official API (partner key). Verify the field mapping in `CVLibrarySource._to_posting`. | `JOBSEARCH_CV_LIBRARY_API_KEY` |
| **Adzuna** | Official UK API; descriptions are snippets. Its distance parameter uses kilometres, converted from the UI's miles. | `JOBSEARCH_ADZUNA_APP_ID`, `JOBSEARCH_ADZUNA_APP_KEY` |
| **Biotechnology Jobs** | Official public JSON Feed (`/jobs.json`): the latest 50 active UK biotech jobs, each with schema.org JobPosting data. CC BY 4.0: every job shows a link back to biotechnologyjobs.co.uk. Polled at most hourly (cached in `data/feed_cache/`). Filtered locally by the user's titles/keywords; UK only | none |
| **Company sites** | Public ATS feeds (Greenhouse, Lever incl. EU, Ashby, Workable, SmartRecruiters, Recruitee, Personio, Teamtailor RSS on its own or the company's domain, Pinpoint); careers-site JSON endpoints within robots.txt (Workday, used by most big pharma; iCIMS; BambooHR); schema.org JSON-LD for other careers pages. Boards are read in parallel (`company_workers`). Workday/iCIMS/SmartRecruiters/BambooHR list first, filter by country server-side where the board allows, and open at most `company_max_details` postings per company whose title fits the search. One entry per board (GSK and ViiV share one) | none: the app finds the boards (see below); add your own companies in the sidebar ("Your companies": name + website, careers page or ATS link; kept across updates) |
| **LinkedIn** (`linkedin_search`) | Public logged-out job search (`jobs-guest` fragments): per term and place up to `linkedin_max_pages` (3) pages of 10 cards, one request per `linkedin_delay_s` (2.5 s), date window as `f_TPR`, radius as `distance`; with 1-2 accepted arrangements, one pass each (`f_WT`) so cards carry LinkedIn's own workplace type. Cards only; `enrich` opens at most `linkedin_max_details` (60) shortlisted postings. The first refusal (429/999) stops the source and keeps what was found. Same ids as LinkedIn alerts (`linkedin:<id>`), so they merge. | none |
| **Totaljobs** | Public search page's embedded result list, page 1 per term and place (robots.txt disallows paging a radius search). Snippets (~300 chars); posting pages refused connections when checked, so `enrich` gives up after one failure. | none |
| **jobs.ac.uk** | Public search (UK-wide: its location filter needs a Google place id), 2 pages of 25 per term; cards carry "Date Placed" and "Closes" (day + month; the year is inferred). `enrich` reads the posting's JSON-LD. | none |
| **NHS Jobs** | Public XML search API (`/api/v1/search_xml`): keyword, location, distance, up to 3 pages; carries `closeDate`. `enrich` reads the advert's overview and description sections. Hourly bank rates are not treated as salaries. | none |
| **LinkedIn, Indeed alerts** | Individually selectable alert emails (`data/inbox/*.eml`) and user-saved postings (`data/inbox/postings/`). Indeed blocks plain HTTP clients, so this (and Gmail alerts) is its only path. | none |
| **Gmail alerts** | Manual read-only fetch from a dedicated inbox. A normal search returns every alert job within the filters and date window, judged before or not (remembered verdicts make repeats free, so the same search gives the same results). **Search new alerts** (`sources == ["gmail_alerts"]`) returns only jobs this CV has not had judged (`only_new`). | `JOBSEARCH_GMAIL_ACCOUNT`, `JOBSEARCH_GMAIL_CLIENT_ID`, `JOBSEARCH_GMAIL_CLIENT_SECRET`; browser OAuth connection |
| **demo** | `data/examples/jobs.example.json` | none |

**Empty titles + selected CV + smart matching** (not alert-only): the profile
summary is built (or reused from memory, keyed by CV id and role family "any") *before*
capture, and `search_tools.board_terms` turns its `target_roles` and `search_keywords` into up
to `BOARD_TERMS` (16) short job-board searches (Reed, CV-Library, Adzuna): composite roles are
split ("Principal Scientist, Cell Therapy / iPSC" -> "Principal Scientist Cell Therapy",
"Principal Scientist iPSC"); the first variant of every role comes first, then the keywords,
then further variants, so a limit never drops a role family. User-typed titles go through the
same splitting (plain titles are unchanged). `SearchQuery.search_budget()` gives every
(term, location) search its own share of `limit` (at least 20), so the first term cannot use
up a board's quota. Company feeds search the same terms. Local feeds (inbox, Gmail alerts, demo) keep the
user's own filters, because the job_matcher judges every posting they return. The same summary
is reused for screening, and the titles are reported as `SearchOutcome.cv_titles`. Because the
summary is remembered per CV, the same CV always searches the same roles.

An empty `sources` list means **all configured sources**: Reed, CV-Library, Adzuna, company
sites and the local inbox, plus **Gmail alerts once Gmail is connected**. When Gmail alerts run,
the local inbox is dropped unless `data/inbox/` holds files (Gmail already carries the
LinkedIn/Indeed alerts). Blank keys in `.env` count as unconfigured. Gmail alerts need a
selected CV and smart matching (alerts are only marked analysed for that CV after screening);
otherwise they are skipped with a reason, or rejected when they are the only source.
Every `SearchOutcome.sources` entry reports each source as `used` (with its posting count before
de-duplication), `failed` (with the error) or `skipped` (with the reason), and the UI shows it.

### Company discovery (BioPharmGuy), run by the app

`services/company_discovery` reads the BioPharmGuy UK list (~1,000 life-science companies) and
finds each company's job feed: known big-pharma Workday boards (`KNOWN_BOARDS`), else the
homepage and up to two hops of careers links (robots.txt checked; single-, double- or unquoted
links) scanned for an ATS link or embed (`detect_boards`); a homepage without a careers link gets
/careers, /jobs, /join-us and /vacancies tried. A board is kept only if it answers; a page with
schema.org JobPosting JSON-LD for at least two jobs is the fallback. Companies without a careers
page are left alone. **Your companies** (`add_company`, sidebar) go through the same search at
once, are kept with no `origin` (never overwritten by discovery) and can be removed.

- **Search:** when "Company career sites" is on and some directory companies were never checked
  (first use, or a pass that was stopped), the search runs discovery first (`mode="new"`,
  about 10 minutes the first time) and streams its progress; Stop keeps what was found and the
  next search resumes. The source is never skipped in the UI for lack of boards.
- **Sidebar:** under the toggle, the board count and last update, and **Update job boards**
  (`POST /api/companies/discover`, `mode="stale"`: unchecked companies plus results older than
  30 days). `GET /api/companies/status` reports progress. One pass runs at a time.
- **Storage:** `data/company_discovery.json` keeps the directory listing and one result per
  company; `data/companies.json` gets the boards merged in: hand-added entries (no `origin`)
  are never replaced, each board and company appears once, and an ATS feed supersedes a
  careers-page entry for the same company.
- Most small biotechs have no readable feed (they post on LinkedIn): expect about one in
  fifteen to be found.

**Source rules (personal tool, within reason):** public job pages only. Never log in, use
session cookies, solve CAPTCHAs, rotate proxies or run a headless browser. Rate-limit every host,
open full postings only for the shortlist, and stop at the first refusal. Careers sites,
directories, Totaljobs and jobs.ac.uk stay within robots.txt; LinkedIn's public search does not
check it (it disallows every crawler) but is slow and capped.

## 1b. Set aside: already applied, N/A (`services/tracker.py`)

`data/job_tracker.json` keeps one entry per role. Before the pre-filter, `tracker.set_aside`
removes roles the user **applied** for (within `APPLIED_LOOKBACK_DAYS` = 365) and roles marked
**N/A**: they cost no model calls and never take a ranked place. They are returned once in
`MatchReport.applied` / `.dismissed` (UI tabs "Already applied" / "N/A") with the reason.

- Same role = same posting id or URL, or same employer (`company_key`) with the same title, or
  the same role words (`title_core`) at the same seniority. **Never skip on the employer
  alone:** another role there is ranked, with a one-line `related` note.
- An application older than the look-back is history: the re-advertised role is ranked again.
- Applied comes from the job card (Applied / N/A / Open), the Applications dialog (including
  applications made outside the app), the agents' `track_job`, and **tailoring a CV** for the
  job (the user's rule: a tailored CV means applied; set it back to Open if not).
- `tracker.annotate` gives every match and "not selected" job a status: **New** (first search it
  appears in) or **Open** (seen before), plus the user's **note**, which the app never rewrites.
  Jobs only ever seen are forgotten after 180 days; applied / N/A entries are kept.
- `GET /api/tracker` (register), `POST /api/tracker` (manual application),
  `PUT|DELETE /api/tracker/{id}`, `PUT /api/jobs/{job_id}/tracking`.
- **Outcomes:** an application records dated `stages` (screening, interview, final_round,
  offer, accepted, rejected, no_response, withdrawn); any decision may carry the user's
  `reason` (why it was or was not suitable). The verdict's `fit_score` and the result's role
  `family` are kept at decision time.
- **Outcome review** (`calibration.review_outcomes`, `GET /api/outcomes/review`, agent tool
  `review_outcomes`): funnel by stage, applications quiet for 14+ days, and patterns with at
  least 3 cases: words recurring in the reasons for ruling out matches of fit ≥ 75, families
  ruled out repeatedly, families with 3+ answered applications and no interview. Read-only:
  it suggests intent or family edits; the user (directly, or through the assistant)
  makes them. A few rejections are noise, and the suggestions say so.

## 2. Pre-filter (deterministic)

**Hard exclusions** (never shown as matches, always reported with the reason):

| Rule | Excluded when |
| --- | --- |
| Closed | `closes_at` (JSON-LD `validThrough`, Reed `expirationDate`, jobs.ac.uk "Closes", NHS `closeDate`) is before today |
| Certification | A `required_certifications` entry the candidate doesn't hold (CV searches only) |
| Work arrangement | Not in the filter's / CV's accepted arrangements |
| Location | Non-remote, in a **known different country**, not radius-verified, and no relocation. A town we cannot place is kept (distance unverified) for the job_matcher to judge |
| Salary | The job's known maximum is below the salary floor (never an estimated salary: `salary_maybe_estimated`) |
| Seniority | Only when the user sets a minimum or maximum job level in the sidebar. With both unset, no role is excluded for its level, whatever title the CV currently holds. The job_matcher still judges scope and fit. |
| Language | The posting states a language as essential (fluent German, native Japanese speaker, ...; not "desirable" or "a plus") that the candidate has **not declared** at all. Only when languages are declared (career intent, else the CV's list); a CV read in English counts as professional English |

**Eligibility flags** (`scorer.eligibility_flags`, `MatchResult.flags`; never exclusions):
a required language above the declared level, or an undeclared one when none are declared;
right to work / no visa sponsorship, security clearance, driving licence and professional
registration, unless confirmed in the career intent's `eligibility`. Each flag quotes the
posting's sentence; the UI lists them under "Check before applying". Only explicitly stated
requirements count: the language a posting is written in is not a requirement.

**Pre-filter score (0–100)** ranks the shortlist. Weights: title 25 · skills 35 · experience 20 ·
location 10 · semantic 10. Skills come from the posting's lists or, when absent (most API and
ATS postings), are **extracted from the description** (`search_tools.extract_skills`).
Location: same town or county ("Oxford" ~ "Oxfordshire") or radius-verified 1.0, same country
only 0.4, unknown 0.7, unplaced town 0.3 (distance not verified).
Searches **without a CV** drop skills and experience and renormalise the rest.
Every eligible posting is scored (no retrieval cap unless `JOBSEARCH_RETRIEVAL_TOP_K` is set);
what goes to screening is described in §5.

**Role-word preference** (`search_tools.shares_role_words`): a title's role-specific words are
its `title_core` minus words every department uses (head, lead, group, global, project, data,
research, ...). A target made only of such words ("Business Development Manager") matches a
title containing all of its core words instead. "Group Leader Stem Cell Research" -> {stem, cell}; "Principal Scientist Cell
Therapy" -> {scientist, cell, therapy}. Postings whose title shares one with a target title
come first at **retrieval** (if a `retrieval_top_k` cap is set) and in the **shortlist**,
where all of them are screened; the others follow by similarity / score. So "Group Leader - Holiday Camp" or a pharma's credit
controller never takes the place of a scientist role. Capture merges postings with the same
title and near-identical full descriptions across employer labels. The shortlist keeps one
posting per (employer, title), with known acronym aliases handled by `company_key`
("UKRI" = "UK Research and Innovation", "GSK" = "GlaxoSmithKline").

**Company feeds** search the same terms as the job boards (the CV's target roles when no title
is typed). Workday, iCIMS and SmartRecruiters search them server-side (one page per term for
Workday) and only open postings that pass `SearchQuery.is_loosely_relevant` (a shared role
word, or a keyword); feed ATSs apply the same filter to their full list.

UI filters override the CV's preferences: titles become target titles, and locations,
arrangements and salary floor come from the form.

**Country** (`SearchQuery.country`, e.g. "United Kingdom"): cities in `locations` are searched
within it (`place_names()` gives "Oxford, United Kingdom", or the country alone). Adzuna searches
that country's API (19 countries); Reed and CV-Library are UK-only and report other countries as
not searched. A country-only search marks board results as inside the search area, and a town
without a country scores "unknown" (0.7), not "unverified".

**History** (`src/services/history.py`): every `run_search` is recorded with its full results in
`data/search_history.json`, newest first, capped at 10. `GET /api/history`, `GET
/api/history/{id}` (reopens it as the current report, so its jobs can be tailored again),
`DELETE /api/history/{id}` and `DELETE /api/history`. Each entry records `models` (provider,
matcher, profile model, effort) when the job_matcher ran, so runs can be compared.

**Source yield** (`history.source_yield`, `GET /api/sources/yield`): every search (and every
Continue, replacing its row) logs, per source, the unique postings it supplied and the true
matches among them in `data/source_yield.json` (last 200 searches). Company feeds count as
"company"; alert postings by origin (`linkedin_alert`, `indeed_alert`); a board searched without
result counts 0. The UI's "Source yield" table shows searches, postings, matches, match rate and
the last match per source, so sources that never produce a match can be switched off.

## 3. Enrich

Shortlisted postings with snippet-only descriptions (< 600 chars) from a source that supports
`enrich` get their full text fetched, then the shortlist is re-scored.

## 4. Career intent, profile summary and role families (quality model, remembered)

**Career intent** (`SearchIntent`, `services/intent.py`, `data/search_intent.json`, one per CV;
`GET/PUT /api/intent`; the Profiles dialog's "Career intent" tab; the assistant's
`update_search_intent` on the user's word): direction, work they want more / less of,
**target areas** to explore (e.g. business development, venture investment), preferred and
avoided sectors, organisation types, soft dealbreakers, working languages with levels and
confirmed eligibility. It is what the user *wants*, never evidence of what they *can do*.
It orients the summary's role families, sets each verdict's `alignment`, supplies languages
and eligibility to the pre-filter, and is the only source of a cover letter's motivation.
Profiles built before an intent change are listed with `intent_changed`.


`profile_memory.summarize_profile(cv, query, llm, memory)` returns a `ProfileSummary`:
headline, seniority, years, core expertise (direct experience), key skills with level
(expert / proficient / familiar) and evidence, domains and sectors, leadership, qualifications,
achievements, **transferable strengths** (kept apart from direct experience), **target roles**
(2-4 obvious + 2-4 adjacent families, as advertised titles), stretch roles, **not_a_fit** role
types, and a narrative. Fields added later default to empty in stored profiles until updated.

- Built only from CV facts (and the search and career intent). The CV is given dated, with
  each role's length, bracketed ids for roles, bullets and projects, education, languages and
  location (`profile_cv_text`), so seniority and years rest on dates. It is the single
  description of the candidate that every screening batch sees, which keeps verdicts consistent.
- **Role families** (`ProfileSummary.role_families`): the roles to search, grouped and
  tiered: **core** (the work they do now), **progression** (the next level) and **adjacent**
  (a different function the experience credibly carries into, e.g. a scientific leader into
  business development or investment). Each has advertised titles, domain terms, CV
  `evidence` ids, the main `gap` and a `rationale`. Every career-intent target area becomes a
  `requested` adjacent family. **LLM proposes, code decides** (`check_families`): evidence ids
  must exist in the CV (adjacent ≥ 2, progression ≥ 1), non-core families must state a gap,
  titles are never removed solely for their distance from the CV's current title, at most
  4 core / 2 progression / 3 adjacent model-suggested families, and without a CV only
  core families. **Code never rejects a family on title words** (e.g. `not_a_fit` naming
  "Clinical Scientist" must not drop every "Scientist" family): whether a role fits is the
  job_matcher's verdict on each posting's requirements, where `not_a_fit` informs it. A
  rejected family keeps its `rejected` reason (shown to the user) and is never searched or
  shown to the job_matcher. Stored profiles are re-checked on use (`ProfileMemory.recheck`,
  no LLM call) unless the user edited them; edits are kept as they are. If no core family
  can be searched, the boards search the profile's `target_roles` and the progress log says
  so, rather than searching only the next level up.
- **Planning** (`family_terms`, used when titles are empty): job-board terms per family,
  budgeted core 60% · progression 20% · adjacent 20% (rescaled over the tiers searched),
  every family keeping its first title. Adjacent families run when requested, or for all
  realistic ones with `SearchRequest.widen` (UI "Widen to adjacent roles"). Summaries
  without families fall back to `target_roles` + `search_keywords`.
- **Attribution and yield**: every result carries the `family` its title belongs to
  (`family_of`), and each search logs found / matches per family in
  `data/family_yield.json` (`history.family_yield`, shown in the Profiles dialog). A family
  with no true match after 3 searches is flagged **not landing**: the realism check, measured
  against the job_matcher's own verdicts.
- **Memory:** keyed by the selected CV's id (`ProfileRecord.cv_id`; the content hash only for
  CVs without an id) + `role_family` (normalised title words). The same CV searched for the
  same kind of role reuses the stored summary (`data/profile_summaries.json`). Editing the CV
  does **not** rebuild it: the record keeps the `cv_fingerprint` it was built from and is listed
  with `cv_changed`. It is rebuilt only when the user asks (`refresh`, or
  `POST /api/profiles/refresh/{key}`). A different CV file has a different id, so it gets its
  own new profiles. Records stored under a content hash are adopted by the matching CV id.
  Each record shows the CV's file name (`cv_name`) and when it was created (`created_at`;
  `updated_at` after an edit or update).
- **User control** (Profiles dialog; `GET/PUT/DELETE /api/profiles`): the user can view the
  selected CV's stored profiles, edit one (`ProfileRecord.edited` is set; it is never rebuilt
  silently), update one from the CV, delete one, or **pin** one (`SearchRequest.profile_key`).
  A pinned profile replaces the remembered default for both the CV-derived titles and
  screening; it must belong to the selected CV, otherwise the search is rejected.
- **Learned preferences** (`services/learning.py`, `data/learned_preferences.json`, per CV):
  "Your labels" > *Suggest profile updates* asks the quality model to turn your labels and
  notes into general preferences: `requirement_gap`, `seniority_floor`, `not_a_fit` (added to
  `not_a_fit`), `target_role`, `transferable_strength`, and `adjacent_family` (a role family
  searched when widening; accepting it adds it to the intent's target areas). Code checks each
  one (cites labels pointing its way, names no employer, a gap never names a profile skill, no
  repeats, families pass `check_families`); the user accepts, rewords or rejects it. Accepted
  ones are applied by `get_summary` and `pinned_profile` to every summary a search uses
  (`learned_for`), so they survive rebuilds; changing them re-judges remembered verdicts.
  `model_compare` measures them on the labels (`learned = true`, `labels_since`).
- **Progress:** every stage emits `search_progress` events (CV, profile, each source with its
  task and posting count, pre-filter, descriptions, screening batch by batch, done). The UI's
  Search button streams them from `POST /api/search/stream` (NDJSON, with `model_usage` events).

## 5. job_matcher: semantic screening

`screener.screen_jobs(summary, shortlist, llm, query, ..., threshold, base, cache, model)`
sends compact posting digests in small batches (`JOBSEARCH_SCREEN_BATCH_SIZE`=3,
`JOBSEARCH_SCREEN_CONCURRENCY`=6) with the candidate's base (CV/filter locations,
arrangements, relocation). Every batch is a separate worker call carrying only the profile,
the constraints and its own postings, so no context grows with the size of the search.
**LLM proposes, code decides:** the model returns a `JobAssessment` per posting (evidence
first, then levels) and `screener.finalize` computes the `JobVerdict`.

**What gets screened** (`search_service._shortlist`, no fixed size): one posting per
(employer, title); **every** posting whose title shares a role word with the target roles,
then the `JOBSEARCH_SCREEN_EXTRA` (10) best-scoring others, up to the safety ceiling
`JOBSEARCH_SCREEN_MAX_JOBS` (250; the progress log says when it is reached). Without target
roles, the best `JOBSEARCH_SCREEN_UNTARGETED` (40).

**Rated levels, not free points.** Each dimension is rated 0-4 against level descriptions in
`MATCHER_SYSTEM` (when torn, the lower); code converts a level to `level/4` of the weight,
rounded half up (30: 0, 8, 15, 23, 30). Choosing a described level is far more repeatable than
choosing a number out of 30.

| Dimension (`FitRatings` -> `FitDimensions`) | Weight | Level 4 … level 0 |
| --- | --- | --- |
| `function` | 30 | does this work now … a different discipline |
| `domain` | 20 | same field, subject matter or technology … unrelated (cross-functional roles: the subject matter they work on) |
| `seniority` | 20 | same level and scope … far off |
| `leadership` | 10 | same kind and size … a kind never done that the role centres on |
| `sector` | 10 | the industry and organisation type of the profile's domains; 3 = another organisation type serving the same industry (supplier, consultancy, investor) … unrelated |
| `practicality` | 10 | easy from the base … unworkable under the constraints |

The prompt is **profile-driven, not tied to one industry**: sectors and domains come from the
profile. A posting in a searched **adjacent** family is never "a different discipline": its
`function` is rated on how much of the work the family's evidence covers (usually 1-2) and
the family's gap is listed. Rejected families are removed from the profile it sees.

**Alignment** (`JobAssessment.alignment`): with a career intent, "aligned" (serves the
direction, energising work, a target area, a preferred sector or organisation type),
"against" (centres on work to avoid, an avoided sector, a soft dealbreaker) or "neutral",
with a short `alignment_note`. It never changes the levels or the fit score: "against" caps
`priority` at consider and lowers the ranking by `ALIGNMENT_PENALTY` (10); the role is
still listed. The career intent is part of the verdict cache key.

**Posting text**: digests carry up to 3,000 characters; a longer posting keeps its opening
and its requirements section (`search_tools.posting_excerpt`), because essential criteria
usually come last.

**Consistency.**
- *Remembered verdicts* (`VerdictCache`, `data/verdict_cache.json`, newest 5,000): keyed by the
  posting's text (not its source id), the profile summary, the constraints, the models
  (provider, screening model and effort, review model and effort) and `MATCHER_VERSION`. The same posting gets the same verdict on
  every search (`from_memory`, "Judged before" in the UI); presentation-only leading
  description labels and known employer aliases share a key, while changed requirements
  are judged again. Bump `MATCHER_VERSION` whenever the prompt or scale changes.
- *Second opinion*: every first-pass match (a lenient first pass can miss an unmet essential
  and put a weak job at the top), a posting within 5 points of the threshold, or one scoring 65–80 with
  ambiguous seniority and leadership (both levels 2–3), gets one more, single-posting
  assessment by the **quality model** (`review_llm`); each level becomes the mean of the two, rounded down, with blockers from either
  reading kept (`combine`, `reviewed`, "Checked twice"). Remembered, it is not asked again.
- *Fixed batches*: postings are batched in a fixed order (by cache key), and the prompt says to
  judge each posting on its own, so neighbours in a batch do not shift a score.

The assessment also carries `fit_summary` (how and how well it matches), `reasons` (direct CV
evidence), `transferable` (related experience that carries, never passed off as direct),
`gaps`, `essential_unmet` (only requirements the posting states as essential), `unknowns`
(what the posting does not say; silence is not a gap), and `dealbreakers`.

`finalize` (code): `borderline` = within 5 points of the threshold (either side) and no
dealbreaker, after the second opinion; the UI flags them. `fit_score` = sum of dimensions, **capped at 65** when an essential is unmet
or any dimension is below 40% of its weight (`cap_reason` says which). `band`: 90+ exceptional ·
80-89 very_strong · 70-79 strong · 60-69 stretch · < 60 weak. `priority`: apply_now (80+) ·
worth_applying (70+) · consider (60+) · low (below, or any dealbreaker). `match` =
`fit_score` ≥ threshold and no dealbreakers.

**Speed and control.** Small batches keep each answer short, so the slowest call ends sooner;
the prompt also caps every explanation list at 3 short points (`MAX_POINTS`, trimmed in code).
A batch with no answer within `JOBSEARCH_SCREEN_BATCH_TIMEOUT_S` (240) is abandoned (its CLI
process killed via `core/llm/calls.cancel_group`) and retried once. A search started with
`SearchRequest.run_id` can be stopped (`POST /api/search/stop/{run_id}`): sources not yet
searched and batches not yet started are skipped, in-flight model calls are killed or
abandoned, and the partial outcome is returned and recorded with `cancelled` and `unscreened`
(shortlisted jobs without a verdict). `continue_search` (`POST /api/search/continue/stream`)
judges only those, keeps the existing verdicts, and updates the history entry in place.

**Final buckets** (`apply_verdicts`): `matches` = `match`, best fit first (threshold default
**60**, the v2 listing floor). `below_threshold` ("Not selected") = everything else that was
scored, still with its full explanation. `excluded` = hard exclusions. If no LLM is configured,
or screening fails, the report falls back to pre-filter scores and says so (`smart_unavailable`).

## 6. Reporting

For each true match give: title, company, location, salary, fit score and band, the
fit_summary, 1–2 direct reasons, transferable points if relevant, the main gap or unknown, and
the URL. Then counts (fetched / matches / not selected / excluded) and any source
problems worth fixing. Never present a rejected or below-threshold job as a match. If nothing
truly matches, say so and suggest what to relax: `widen` (the profile's realistic adjacent
families), a wider radius, a lower salary floor, or more sources. Name each match's role
family (say plainly when it is a move into a new function, with its gap), its alignment
when it goes against the career intent, and its eligibility flags.

Deterministic CLI (no LLM):
```bash
python .agent/skills/job_search/scoring_engine.py --cv data/master_cv.json --jobs data/jobs.json --show-rejected
```
