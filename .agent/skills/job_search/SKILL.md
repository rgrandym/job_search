---
name: job_search
description: Capture job postings (Reed, CV-Library, LinkedIn/Indeed alerts and saved postings, company career sites) using title/location/radius/salary filters, pre-filter them on hard constraints, and select the ones that truly match the candidate using a remembered profile summary and the job_matcher subagent's semantic judgement. Use when the user wants to find, import, score or rank jobs.
---

# job_search

Pipeline (`src/services/search_service.py::run_search`, shared by the UI, the API and the agents):

```
filters ─▶ 1 capture ─▶ 2 pre-filter ─▶ 3 enrich ─▶ 4 profile summary ─▶ 5 job_matcher ─▶ ranked true matches
           sources      deterministic    full text    orchestrator,        semantic verdict
                        (hard rules)     for short-   remembered per       per posting
                                         list         CV + role family
```

**Matching philosophy.** Keyword overlap is not a match. The deterministic scorer only removes
what is certainly wrong (hard constraints) and orders a shortlist cheaply. The decision about
what *truly* matches is made by the **job_matcher** subagent, which reads each posting like a
senior recruiter against an evidence-based **profile summary** of the candidate.

| File | Role |
| --- | --- |
| `src/jobs/sources/` | One adapter per source (§1) |
| `src/jobs/scorer.py`, `matcher.py` | Hard exclusions + pre-filter score (§2) |
| `src/jobs/profile_memory.py` | Orchestrator's profile summary + memory (§4) |
| `src/jobs/screener.py` | job_matcher subagent: batch semantic screening (§5) |
| `job_schema.json` | Generated JSON Schema for `JobPosting` lists |
| `scoring_engine.py` | CLI: deterministic pre-filter report for a CV + jobs file |

---

## 1. Capture (search filters)

`SearchQuery` = the UI form: `titles`, `keywords`, `locations`, `distance_miles`, `salary_min`,
`salary_max`, `work_arrangements`, `sources`.

- Sources with server-side search (Reed, CV-Library) run **one query per title**
  (`SearchQuery.search_terms()`), with location, radius and salary passed to the API. Their
  results are marked `within_search_area` so radius-matched towns aren't mistaken for mismatches.
- Local sources (company feeds, inbox, demo) apply a loose recall filter (`is_relevant`):
  title-family overlap (abbreviations expanded: "Sr. ML Eng" = "senior machine learning
  engineer") or any keyword.

| Source | How | Setup |
| --- | --- | --- |
| **Reed** | Official API. Search returns snippets; full text is fetched lazily for the shortlist (`ReedSource.enrich`). | `JOBSEARCH_REED_API_KEY` |
| **CV-Library** | Official API (partner key). Verify the field mapping in `CVLibrarySource._to_posting`. | `JOBSEARCH_CV_LIBRARY_API_KEY` |
| **Company sites** | Greenhouse / Lever / Ashby public feeds; schema.org JSON-LD for other careers pages (robots.txt checked) | `data/companies.json` |
| **LinkedIn, Indeed** | Alert emails (`data/inbox/*.eml`) and user-saved postings (`data/inbox/postings/`) | none |
| **demo** | `data/examples/jobs.example.json` | none |

**Compliance (non-negotiable):** never scrape LinkedIn or Indeed, log in anywhere, bypass
robots.txt, rate limits or anti-bot measures.

## 2. Pre-filter (deterministic)

**Hard exclusions** (never shown as matches, always reported with the reason):

| Rule | Excluded when |
| --- | --- |
| Certification | A `required_certifications` entry the candidate doesn't hold (CV searches only) |
| Work arrangement | Not in the filter's / CV's accepted arrangements |
| Location | Non-remote, different country, not radius-verified, and no relocation |
| Salary | The job's known maximum is below the salary floor |
| Seniority | Gap ≥ 3 levels (CV searches only) |

**Pre-filter score (0–100)** ranks the shortlist. Weights: title 25 · skills 35 · experience 20 ·
location 10 · semantic 10. Skills come from the posting's lists or, when absent (most API and
ATS postings), are **extracted from the description** (`search_tools.extract_skills`).
Location: same city or radius-verified 1.0, same country only 0.4, unknown 0.7.
Searches **without a CV** drop skills and experience and renormalise the rest.
The top `JOBSEARCH_SCREEN_SHORTLIST_SIZE` (40) go to screening.

UI filters override the CV's preferences: titles become target titles, and locations,
arrangements and salary floor come from the form.

## 3. Enrich

Shortlisted postings with snippet-only descriptions (< 600 chars) from a source that supports
`enrich` get their full text fetched, then the shortlist is re-scored.

## 4. Profile summary (orchestrator, remembered)

`profile_memory.summarize_profile(cv, query, llm, memory)` returns a `ProfileSummary`:
headline, seniority, years, core expertise, key skills with level (expert / proficient /
familiar) and evidence, domains, **target roles** (incl. adjacent titles), stretch roles,
**not_a_fit** role types, and a narrative.

- Built only from CV facts (and the search intent). It is the single description of the
  candidate that every screening batch sees, which keeps verdicts consistent.
- **Memory:** keyed by `cv_fingerprint` (CV content hash) + `role_family` (normalised title
  words). The same CV searched for the same kind of role reuses the stored summary
  (`data/profile_summaries.json`). Rebuild only on request (`refresh`) or when the CV changes,
  which happens automatically because the fingerprint changes.

## 5. job_matcher: semantic screening

`screener.screen_jobs(summary, shortlist, llm, query)` sends compact posting digests in batches
(`JOBSEARCH_SCREEN_BATCH_SIZE`=6, `JOBSEARCH_SCREEN_CONCURRENCY`=4) and gets one `JobVerdict` each:

| Field | Meaning |
| --- | --- |
| `match` | True only for a genuine fit worth applying to |
| `fit_score` | 85–100 strong · 70–84 good · 50–69 stretch · < 50 poor |
| `reasons` / `gaps` / `dealbreakers` | Specific to the posting and the profile |

The matcher judges the **core of the role** against core expertise, real level and scope,
must-haves against *evidenced* skills ("familiar" rarely satisfies a must-have), and domain
transfer. It rejects keyword look-alikes from other disciplines and anything in `not_a_fit`.

**Final buckets** (`apply_verdicts`): `matches` = `match` and `fit_score` ≥ threshold (default 70),
best fit first. `below_threshold` ("Not selected") = everything else that was scored.
`excluded` = hard exclusions. If no LLM is configured, or screening fails, the report falls back
to pre-filter scores and says so (`smart_unavailable`).

## 6. Reporting

For each true match give: title, company, location, salary, fit score, 1–2 reasons, the main
gap, and the URL. Then counts (fetched / matches / not selected / excluded) and any source
problems worth fixing. Never present a rejected or below-threshold job as a match. If nothing
truly matches, say so and suggest what to relax: adjacent titles from `target_roles`, a wider
radius, a lower salary floor, or more sources.

Deterministic CLI (no LLM):
```bash
python .agent/skills/job_search/scoring_engine.py --cv data/master_cv.json --jobs data/jobs.json --show-rejected
```
