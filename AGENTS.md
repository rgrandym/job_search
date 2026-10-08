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
**left**: CV library/upload (click a CV to review and edit its parsed facts) and search filters (titles, keywords, location, radius, salary, arrangement,
sources, threshold, smart-match toggle). **centre**: ranked results with AI fit scores, reasons,
gaps, and tailor-to-.docx. **right**: the assistant over `/api/ws/chat`
(plain-language actions through the same services as the buttons). Searching is available
from the sidebar's Search button or assistant (direct pipeline, stoppable). **centre** also
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
The LLM provider (Claude API, Claude Code CLI, OpenAI/Codex, OpenRouter) and a model per task,
each with its own effort, are chosen in Settings. Profile, CV and letter models may run on
another provider than search and matching (e.g. Opus through Claude Code while Codex screens):

| Role | Used for | Calls |
| --- | --- | --- |
| **profile** | explicit profile building and updates (optional; blank = the quality model and its effort; may use its own provider, `profile_provider`) | when requested |
| **cv** | tailored CVs: JD analysis, plan, review and revision (optional; blank = the quality model; `cv_provider`) | per tailored CV |
| **letter** | cover letters (optional; blank = the CV model, else the quality model; `letter_provider`) | per letter |
| **quality** | CV parsing for search, search-time summary on a cache miss, evidence extraction, second opinions on matches and near the threshold, the assistant's default, and writing tasks without their own model | rare, accuracy-critical |
| **screening** | the job_matcher's first pass (batches of 3) | hundreds per search |

## Buttons and assistant share services

Search, screening, tailoring, cover letters, saving and tracking call `src/services/`
directly from buttons or from the single assistant (`src/agents/`). The assistant can
select the configured quality or screening model, another tool-capable model from the
current provider's catalogue, or a connected Claude Code model per turn. Its completed conversation
turns are saved under `data/chat_sessions/` (recent turns with their tool calls and results);
its model calls appear in live usage. Claude Code and Codex models run natively: the CLI's own
agent loop calls the app's tools through `POST /api/mcp/<run token>` (`src/agents/mcp.py`, valid
only during that run) with its built-in shell, file, web and similar tools off, and resumes its
own session on each turn (`src/core/llm/native.py`). API models (Claude, OpenAI, OpenRouter)
use their native tool calling in `runtime.py`'s loop.
`edit_cv` also writes bullet changes to a Word CV's own file (`services/cv_document.py`). An uploaded CV
is one CV: its file and its copy in `output/cvs/` are kept identical (the one changed last wins)
and its id never changes (`data/cv_ids.json`), so its parse, profiles and documents stay attached.

| Tool | Does |
| --- | --- |
| `get_context` | selected CV, saved profiles, search results, saved jobs, preferences and intent |
| `actions.py` / `actions_more.py` tools | search and results, jobs, documents, review queues, profiles, tracker, companies and history via services |
| `update_search_intent` | merge patch of the career intent |
| `update_preferences` | merge patch of the CV's search preferences |
| `update_profile` | edits a saved profile as the user asks (`patch`, or `append` to add list items) |
| `read_cv` / `edit_cv` | the selected CV in full with ids; targeted edits by id (`src/cv/edits.py`: add/reword/remove a bullet, edit/add/remove a role, set a section) |
| `propose_cv_facts` | a long pasted document -> evidence review queue; the user accepts each fact |

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
              (Greenhouse … Workday, iCIMS; SuccessFactors, Phenom, Oracle, Jobvite, CWS
              careers sites; boards found by services/company_discovery,
              run before capture when companies are unchecked)
              · LinkedIn public search · Totaljobs · jobs.ac.uk · NHS Jobs
              · LinkedIn/Indeed alert emails & saved postings · demo           → JobPosting[]
1b set aside  services/tracker: roles already applied for (12-month look-back) or marked N/A,
              and services/labels: postings you labelled "no" (same id, link or role; the note
              is the reason) leave here, before any scoring or model call
              → report.applied / .dismissed
2 pre-filter  scorer.hard_exclusions (closed, arrangement, location, salary floor, certs,
              undeclared essential languages; seniority only when the user sets a level range)
              + eligibility flags (language level, right to work, clearance, licence)
              + pre-filter score (skills from lists or extracted from text)    → shortlist:
              every role-relevant posting + 10 others (ceiling 250), one per employer+title
3 enrich      full descriptions for snippet-only shortlisted postings (Reed, LinkedIn,
              jobs.ac.uk, NHS Jobs, Totaljobs)
4 summary     ProfileSummary (saved profile or search quality model)  ◀── ProfileMemory (data/profile_summaries.json)
              from the original CV document ([src-N] lines, publications included) +
              the dated CV extract + career intent; capabilities and publication record;
              role families (core / progression / adjacent) checked by code (CV or
              document evidence ids and gap; fit is the matcher's); readable copies in
              output/profiles/;
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
─▶ apply_plan (guards: source ids, no new numbers, skills only from the source bullet, summary
skills/numbers in the CV; headline kept, up to 4 checked headline_options offered; every bullet
kept and reordered unless the length choice or assess_trim allows trimming) ─▶ critique_cv (LLM, second reader)
─▶ revise_plan ─▶ apply_plan ─▶ TailoredCV
─▶ docx_original.write_like_original (a copy of the CV's Word design, also for a PDF CV's Word
version; templates only when chosen)
─▶ output/cvs/<Name>_<Company>.docx ─▶ ats.check_docx (read-back)
                                                  └─▶ tracker: remember the CV; only the user marks Applied
job ─▶ cover_letter.draft_letter (LLM; motivation only from the career intent) ─▶ apply_letter
(each paragraph cites CV ids; numbers/skills from them) ─▶ <Name>_<Company>_cover_letter.docx
document ─▶ enrichment.propose (LLM; quotes checked against the document) ─▶ review queue
─▶ the user accepts ─▶ Master CV
```

## Invariants to preserve

- Only `src/core/llm/` and `src/core/llm_provider.py` talk to model APIs.
- Whether a job matches is the job_matcher's semantic verdict, not keyword overlap. Hard
  constraints stay deterministic.
- Tailored CVs keep the original's design, headline (suggestions are offered) and publications,
  and remove nothing unless the user chose a junior length or `tailor.assess_trim` finds a
  clearly more junior role (see CLAUDE.md, Tailored CV Rules).
- Tailoring never invents facts. Don't relax `tailor._fabrication_reason`, the headline and
  summary guards, `cover_letter.apply_letter` or `enrichment.check_proposals`.
- Career intent is what the user wants, never evidence of what they can do. Outcome review
  suggests; it never changes the intent, families or scoring by itself.
- Pydantic models are the source of truth. Regenerate JSON schemas with `export-schema`.
  `web/src/lib/types.ts` mirrors them.
- Public job pages are fine (personal tool): no logins, no anti-bot circumvention, rate-limited,
  full postings only for the shortlist; headless, signed-out Chrome only through
  `sources/browser.py`. See "Job Sources" in CLAUDE.md.
- Ask before adding dependencies (pip or npm). Don't commit unless asked.
