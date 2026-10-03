# AGENTS.md: Operational Context for Codex, Cursor and VS Code Agents

Claude Code users: `CLAUDE.md` has the full rule set (constraints, style, shell policy). Its
**Architectural Constraints**, **No-Fabrication Contract** and **Job Source Compliance**
sections apply to every agent, not just Claude.

## Workspace scope

- Treat this repository as the only workspace. Read, edit, test and run commands only inside it.
- Do not access or modify sibling projects unless the user explicitly expands the scope.
- Preserve existing uncommitted work. Do not revert, overwrite or reformat unrelated changes.
- `data/` (except `data/examples/`) and `output/` hold personal data and API keys. Never commit
  them or paste them into issues or PRs.
- Keep it simple: no audit or trace layers. Invest in search quality and profile matching.

## Setup & verification

```bash
conda activate job_search && pip install -e ".[dev]"
pytest && ruff check src tests .agent && mypy src
cd web && npm install && npm run typecheck && npm run lint && npm run build
bash scripts/dev.sh                                  # (re)start backend + frontend → http://localhost:5173
```

All tests run offline: the LLM is faked, HTTP is mocked, and the agent chat uses a scripted model.

## The app

A three-pane web UI (`web/`) over a FastAPI backend (`src/web/app.py`):
**left**: CV library/upload and search filters (titles, keywords, location, radius, salary, arrangement,
sources, threshold, smart-match toggle). **centre**: ranked results with AI fit scores, reasons,
gaps, and tailor-to-.docx. **right**: the assistant over `/api/ws/chat`
(plain-language updates to the career intent, preferences and proposed CV facts). Searching is
the sidebar's Search button (direct pipeline, stoppable). **centre** also
has a **Saved** tab: jobs ticked and saved from a search (`services/saved.py`,
`data/saved_jobs.json`), kept until removed, with Applied / outcome status read live from
the tracker. Ticked jobs can be saved, or get a tailored CV + cover letter each from the
selection bar; `Workspace.job` / `.result` find a job in the current search or the saved
list, so tailoring and cover letters work on saved jobs from older searches.
Each job card of the current search has **Your call** (Would apply / Maybe / No,
`services/labels.py`, `data/job_labels.json`): a permanent labelled set, each label with a
snapshot of the posting, constraints, profile summary and the verdict with its models. Labels
never change a search; the sidebar's **Your labels** compares each model setup with them
(good jobs kept, bad jobs let through, ranking), and `model_compare` can re-screen them later.
Its **Learn from your labels** section (`services/learning.py`) turns labels and notes into
general preferences (requirements the CV lacks, seniority floor, target roles, transferable
strengths, adjacent role families); the user accepts each, and every search's profile gets them.
The LLM provider (Claude API, Claude Code CLI, OpenAI/Codex, OpenRouter) and two models,
each with its own effort, are chosen in Settings:

| Role | Used for | Calls |
| --- | --- | --- |
| **quality** | profile summary, CV parsing, tailoring plan + review, cover letters, evidence extraction, second opinions on matches and near the threshold, the assistant | rare, accuracy-critical |
| **screening** | the job_matcher's first pass (batches of 3) | hundreds per search |

## Buttons do the work; one assistant edits records

Search, screening, tailoring, cover letters, saving and tracking are buttons that call
`src/services/` directly: no agent sits in those paths. The **assistant** (right panel,
`src/agents/`) is a single agent that turns what the user says into updates of their records:

| Tool | Does |
| --- | --- |
| `get_context` | the selected CV's roles (ids), search preferences, career intent |
| `update_search_intent` | merge patch of the career intent |
| `update_preferences` | merge patch of the CV's search preferences |
| `propose_cv_facts` | the user's words -> evidence review queue; the user accepts each fact |

The **job_matcher** is not a chat agent: it is the screening model inside `run_search`
(`src/jobs/screener.py`). `python -m src.services.model_compare` (or `/compare_models` with `model_compare.toml`)
re-screens every job you labelled yes/no in the app with each candidate setup and scores it
against your labels: agreement, good jobs kept, bad jobs let through, ranking, time and usage
(`reference = "history"` compares with past verdicts instead). It calls the paid model: run it
deliberately.

## Data flow 1: Search & match (the critical path)

```text
UI filters ─▶ SearchQuery          career intent (services/intent, per CV)
        │
        ▼  services/search_service.run_search
1 capture     sources: Reed · CV-Library (1 query per title, radius, salary) · company ATS feeds
              (Greenhouse … Workday, iCIMS; boards found by services/company_discovery,
              run before capture when companies are unchecked)
              · LinkedIn public search · Totaljobs · jobs.ac.uk · NHS Jobs
              · LinkedIn/Indeed alert emails & saved postings · demo           → JobPosting[]
1b set aside  services/tracker: roles already applied for (12-month look-back) or marked N/A
              leave here, before any scoring or model call → report.applied / .dismissed
2 pre-filter  scorer.hard_exclusions (closed, arrangement, location, salary floor, certs, seniority
              except areas the user chose to move into, undeclared essential languages)
              + eligibility flags (language level, right to work, clearance, licence)
              + pre-filter score (skills from lists or extracted from text)    → shortlist:
              every role-relevant posting + 10 others (ceiling 250), one per employer+title
3 enrich      full descriptions for snippet-only shortlisted postings (Reed, LinkedIn,
              jobs.ac.uk, NHS Jobs, Totaljobs)
4 summary     ProfileSummary (quality model)  ◀── ProfileMemory (data/profile_summaries.json)
              from the dated CV + career intent; role families (core / progression /
              adjacent) checked by code (CV evidence ids, gap, level window; fit is the matcher's);
              with empty titles the boards search the families (budget 60/20/20, adjacent
              only when requested or `widen`); results are tagged with their family and
              per-family yield logged (data/family_yield.json, "not landing" after 3 searches)
              key = CV id + role family → same summary for the same type of search;
              CV / intent edits flag it (cv_changed, intent_changed), only a refresh rebuilds it
5 screen      job_matcher: batches of 3, 6 workers, levels 0-4 per dimension (profile-driven,
              not tied to one industry) + alignment with the career intent → code computes
              JobVerdict {match, fit 0-100, reasons, gaps, alignment}; remembered per
              posting+profile+intent+model (data/verdict_cache.json); second opinion on every
              match and within 5 points of the threshold; "against" the intent caps priority, never the score
        │
        ▼
MatchReport {matches (verdict.match & fit ≥ threshold), below_threshold, excluded+reasons,
             applied, dismissed, summary}; every match / not-selected job carries
             tracking {status New|Open, the user's note} (tracker.annotate)
        → UI results panel (REST response or `search_results` WebSocket event)
        → history (last 10 searches) + source-yield log (data/source_yield.json)
```

No LLM configured → steps 4–5 are skipped and the report says it shows pre-filter scores only.

## Data flow 2: Master CV → Tailoring → Word document

```text
upload (.pdf/.docx/.md/.txt) ─▶ data/cvs/ (stored unchanged; no parsing or LLM call)
select CV + request a CV-powered action ─pypdf/python-docx─▶ text ─LLM─▶ MasterCV
job from results ─▶ tailor.analyze_jd ─▶ propose_plan (LLM, steered by the job_matcher verdict)
─▶ apply_plan (guards: source ids, no new numbers, skills only from the source bullet, headline
within the level and roles held, summary skills/numbers in the CV; dropped keyword bullets put
back) ─▶ critique_cv (LLM, second reader) ─▶ revise_plan ─▶ apply_plan ─▶ TailoredCV
─▶ export_docx ─▶ output/<Name>_<Company>.docx ─▶ ats.check_docx (read-back)
                                                  └─▶ tracker: the job is marked Applied (user's rule)
job ─▶ cover_letter.draft_letter (LLM; motivation only from the career intent) ─▶ apply_letter
(each paragraph cites CV ids; numbers/skills from them) ─▶ <Name>_<Company>_cover_letter.docx
document ─▶ enrichment.propose (LLM; quotes checked against the document) ─▶ review queue
─▶ the user accepts ─▶ Master CV
```

## Invariants to preserve

- Only `src/core/llm/` and `src/core/llm_provider.py` talk to model APIs.
- Whether a job matches is the job_matcher's semantic verdict, not keyword overlap. Hard
  constraints stay deterministic.
- Tailoring never invents facts. Don't relax `tailor._fabrication_reason`, the headline and
  summary guards, `cover_letter.apply_letter` or `enrichment.check_proposals`.
- Career intent is what the user wants, never evidence of what they can do. Outcome review
  suggests; it never changes the intent, families or scoring by itself.
- Pydantic models are the source of truth. Regenerate JSON schemas with `export-schema`.
  `web/src/lib/types.ts` mirrors them.
- Public job pages are fine (personal tool): no logins, no anti-bot circumvention, rate-limited,
  full postings only for the shortlist. See "Job Sources" in CLAUDE.md.
- Ask before adding dependencies (pip or npm). Don't commit unless asked.
