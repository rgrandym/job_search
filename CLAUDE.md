# AI Job Search Engine & CV Tailor: Claude Code Instructions

## Project

**Backend:** Python 3.11+ (env runs 3.12) · FastAPI + WebSocket · Pydantic v2 · pydantic-settings · httpx · python-docx · pypdf
**Frontend (`web/`):** React 18 · TypeScript (strict) · Vite · Tailwind (CSS tokens in `index.css`) · Zustand · TanStack Query · Lucide · react-resizable-panels
**Conda env:** `job_search`. Always activate it before running Python commands.
**LLMs:** Claude (Anthropic SDK), OpenAI or OpenRouter (Chat Completions), selected in the UI. Separate
quality model (CV and letters, second opinions, assistant), screening model (job matching) and an
optional profile model (profile summary; blank = quality model; may use another provider), each with its own effort. All model access goes through `src/core/llm/`.
**Scope (user preference):** keep it simple. No audit or trace infrastructure. Effort goes into
search quality and profile matching.

Read `AGENTS.md` for the data flows. Read the skill docs before touching their area:
`.agent/skills/cv_writer/SKILL.md`, `.agent/skills/job_search/SKILL.md`.

## Commands

```bash
conda activate job_search
pip install -e ".[dev]"                     # first time only

pytest                                       # all tests (offline; fake LLM + mocked HTTP)
pytest tests/test_job_search.py -k reed      # one area
ruff check src tests .agent && ruff format src tests .agent
mypy src                                     # strict mode, must stay clean

python -m src.cv.master_cv_manager validate data/master_cv.json
python -m src.cv.master_cv_manager export-schema     # after changing src/cv/models.py
python -m src.jobs.fetcher export-schema             # after changing src/jobs/models.py
python -m src.jobs.fetcher fetch --keywords "ml engineer" --out data/jobs.json
python -m src.services.company_discovery [--stale | --all]   # same pass the app runs (Update button)
python .agent/skills/job_search/scoring_engine.py --cv data/examples/master_cv.example.json \
    --jobs data/examples/jobs.example.json --show-rejected
python .agent/skills/cv_writer/docx_templates.py --cv <cv.json> --template classic --out output/cv.docx

# Web app
bash scripts/dev.sh                    # (re)start backend :8000 + frontend :5173; Ctrl-C stops both
bash scripts/dev.sh --stop             # stop both · --install forces dependency reinstall
# logs: .run/logs/{backend,frontend}.log
cd web && npm run typecheck && npm run lint && npm run build
```

## Layout

```
src/
  core/        config.py (Settings) · llm_provider.py (protocols, embedder, factory)
               progress.py (live task steps + in-flight model calls, polled by the UI)
    llm/       types.py · anthropic_backend.py · openai_backend.py (OpenAI + OpenRouter)
  cv/          models.py · master_cv_manager.py · tailor.py (guards + review) · cover_letter.py
               ats.py (docx read-back) · docx_exporter.py
  jobs/        models.py · fetcher.py · matcher.py + scorer.py (pre-filter)
               profile_memory.py (summary, role families, memory) · screener.py (job_matcher)
    sources/   base.py (HTTP, robots, JSON-LD) · job_boards.py (Reed, CV-Library, Adzuna)
               feeds.py (Biotechnology Jobs public feed)
               public_boards.py (LinkedIn public search, Totaljobs, jobs.ac.uk, NHS Jobs)
               companies.py (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio,
               Teamtailor, Pinpoint, Workday, iCIMS, BambooHR, careers pages) · directories.py (BioPharmGuy)
               inbox.py (LinkedIn/Indeed alerts, saved postings)
               pages.py (a posting's own page: JSON-LD full text, robots.txt kept)
               browser.py (headless signed-out Chrome for postings still too thin)
  tools/       search_tools.py (pure text utils) · docx_tools.py (python-docx primitives)
  services/    workspace.py (state) · search_service.py (THE search pipeline) · cv_service.py
               tracker.py (applied / N/A / New / Open + outcome stages, data/job_tracker.json)
               history.py (+ source and role-family yield) · intent.py (career intent, per CV)
               calibration.py (read-only outcome review) · enrichment.py (evidence review queue)
               saved.py (saved jobs, data/saved_jobs.json; status from the tracker)
               labels.py (your yes/maybe/no per job + snapshot, data/job_labels.json; measures models;
               a "no" is set aside in later searches)
               learning.py (preferences learned from labels and applied/N/A reasons; new reasons are
               added automatically at the start of a search, the user can remove any)
               model_compare.py (CLI: score model setups against your labels) · compare_usage.py
               (its cost per model, plan usage and progress bar)
               company_discovery.py (directory -> data/companies.json; run by searches + Update button)
  agents/      the assistant: registry.py · runtime.py (loop) · tools.py · definitions.py · prompts/assistant.md · chat.py
  web/         app.py (REST + /api/ws/chat + serves web/dist)
web/src/       App.tsx · components/ · stores/ (zustand) · lib/ (api.ts, types.ts mirror the models)
.agent/skills/ SKILL.md + generated JSON schemas + thin CLI entrypoints
.agent/agent_configs/  tool-agnostic agent definitions
data/examples/ committed fixtures · data/* and output/ are git-ignored (personal data)
```

## Architectural Constraints (MANDATORY)

1. **LLM isolation.** Only `src/core/llm/` and `src/core/llm_provider.py` talk to model APIs
   (the Anthropic SDK, or httpx for OpenAI-compatible APIs). Everything else depends on the
   `LLMProvider` / `ChatModel` / `Embedder` protocols and gets an instance passed in
   (`Workspace.structured(role)` / `.chat(role)`). Never construct a client in domain code.
2. **Structured outputs.** Domain LLM calls are `llm.generate(system=..., prompt=..., output_model=Model)`.
   Only the agent loop uses free-form chat with tool calling.
3. **Who decides what.** Hard constraints (location, arrangement, salary floor, certifications,
   seniority gap) are deterministic (`scorer.hard_exclusions`). **Whether a job truly matches is
   the job_matcher's semantic verdict** against the profile summary, not keyword
   overlap. The pre-filter score only builds the shortlist. CV tailoring stays "LLM proposes,
   code decides" (`tailor.apply_plan`).
4. **One path per action.** Search, summary, tailoring and CV import live in `src/services/`.
   REST routes, agent tools and CLIs call them. Never re-implement a pipeline in a route or tool.
5. **Models are the source of truth.** `master_cv_schema.json` and `job_schema.json` are generated.
   Change the Pydantic model, run `export-schema`, commit both. A test fails on drift.
6. **Skill scripts are thin.** `.agent/skills/**.py` only parse args and call `src/`. Logic goes in `src/`.
7. **Layering:** `tools` ← `core` ← `cv` ← `jobs` ← `services` ← `agents` ← `web`. `tools/` is pure. No upward imports.
8. **Matching contract.** Changes to exclusions, pre-filter weights, the summary or matcher prompts,
   or thresholds must update the code, `.agent/skills/job_search/SKILL.md`, the agent configs and the tests **together**.
9. **Buttons first, one assistant.** Search, screening, tailoring, cover letters, saving and
   tracking are buttons calling `src/services/`; no agent sits in those paths. The assistant
   (`src/agents/`) only updates records the user describes (career intent, preferences, proposed
   CV facts). Add one of its tools with `@tool(name, description, ArgsModel)` in
   `src/agents/tools.py` (thin wrapper over a service) and list it in `definitions.py`. Frontend
   types in `web/src/lib/types.ts` mirror the backend models. Update both together.

## No-Fabrication Contract (MANDATORY)

A CV is a factual document. Tailoring may rephrase and reorder. It may never invent.
- Every tailored bullet carries `source_id` → a Master CV bullet. Unknown ids are rejected.
- `apply_plan` rejects rewrites that add numbers, or add JD skills that the Master CV doesn't evidence.
  **Do not weaken these guards** to make a test or a demo pass. Fix the prompt or the data.
- Every accept/reject is recorded in `TailoredCV.changes` with a reason. Surface rejections and
  `missing_keywords` to the user.
- New facts go into the Master CV (after the user confirms them), never straight into a tailored CV.

## Job Sources (MANDATORY)

Personal job-search tool: public job pages may be read, within reason.

- Official APIs (Reed, CV-Library), public ATS feeds (Greenhouse, Lever, Ashby, Workable,
  SmartRecruiters, Recruitee, Personio, Teamtailor RSS, Pinpoint), JSON endpoints that a careers site's own pages call
  (Workday `/wday/cxs/`, iCIMS job lists, BambooHR `/careers/list`) **when that host's `robots.txt` allows them**, or
  JSON-LD from careers pages whose `robots.txt` allows us. All HTTP goes through
  `sources.base.HttpFetcher` (User-Agent, per-host delay, robots checks); careers-site endpoints
  always pass `check_robots=True`. Open postings one by one only for titles that fit the search,
  capped by `company_max_details`.
- A shortlisted posting still too thin to check its requirements may have its own page read
  once (`sources/pages.PostingPages`: JSON-LD only, `check_robots=True`, capped by
  `page_max_details`; never LinkedIn or Indeed hosts).
- Company directories (BioPharmGuy) are read once per discovery run, never per search, and only
  pages their `robots.txt` allows.
- **Public job boards** (`sources/public_boards.py`): LinkedIn's logged-out job search, Totaljobs,
  jobs.ac.uk, NHS Jobs. List result cards per search; open full postings only in `enrich` (the
  shortlist). LinkedIn ignores robots.txt but is slow (`linkedin_delay_s`), capped
  (`linkedin_max_pages`, `linkedin_max_details`, `linkedin_max_search_requests`) and waits when asked to slow down (429/999, honouring Retry-After; `linkedin_cooldown_s`, at most `linkedin_max_cooldowns` per search) and only then gives up; the
  others stay within their robots.txt.
- **Headless Chrome (user's decision, 2026-10-07):** a shortlisted posting still too thin to
  check its requirements may be opened in the user's installed Chrome, headless, through
  `sources/browser.BrowserFetcher` only: a fresh empty profile per page (signed out, never the
  user's profile or cookies), robots.txt kept except for LinkedIn and Indeed postings (opened
  slowly, `browser_delay_s`), at most `browser_max_pages` per search, Stop kills it. A
  challenge or sign-in wall ends that host for the search (the user can paste the text).
  Chrome CLI only: no Playwright or other new dependency without asking.
- **Never:** logins or session cookies, CAPTCHA solving, proxy rotation, disguising the
  browser's identity, or other anti-bot circumvention. Indeed search is still reached through
  the user's alert emails (`data/inbox/*.eml`, Gmail alerts) and saved postings.
- New source = new adapter in `src/jobs/sources/` implementing `fetch(SearchQuery) -> list[JobPosting]`,
  registered in `fetcher.build_sources` and listed in `fetcher.SOURCE_CATALOG` (label, category:
  job_boards / company / alerts; the UI's source panels are built from it), tested with
  `httpx.MockTransport`.
- Unverified third-party field mappings must say so in the docstring (see `CVLibrarySource`).

## Code Style

- Python 3.11+ syntax (`X | None`, `list[str]`), `from __future__ import annotations` at the top of every module.
- **Strict typing:** type hints on every signature; `mypy --strict` clean. Pydantic v2 models for all
  data crossing a module boundary, with `extra="forbid"` on domain models.
- Docstrings: module docstring stating purpose; one-line docstrings on public functions/classes
  (add Args/Returns only when non-obvious). Comments explain *why*, not *what*.
- `PascalCase` classes, `snake_case` functions/variables, `UPPER_SNAKE` module constants.
- Functions ≤ 60 lines. Refactor if a change would push past that.
- Config via `get_settings()` only. Never read `os.environ` directly (sole exception: ambient
  Anthropic credential detection in `src/core/llm/anthropic_backend.py`). Secrets are `SecretStr`.
- Ruff (line length 100) is the formatter. Match existing style exactly.
- **Frontend:** strict TS, function components and hooks, server state in TanStack Query, UI and
  session state in Zustand stores. Colours only via the tokens (`bg-panel`, `text-muted`,
  `var(--accent)` …), never hard-coded hex. Every view has explicit loading, empty and error states.

## Editing Rules

- Minimal edits: the smallest change that solves the problem. Rewrite only when asked.
- Every behaviour change ships with a test. Tests are offline: use `tests/conftest.FakeLLM`,
  `httpx.MockTransport`, and `ScriptedChat` (in `tests/test_webapp.py`) for the agent loop.
- **Always ask before adding new dependencies.** Then add them to `pyproject.toml`.
- No commits unless explicitly instructed.

## Protected: Never Modify or Commit

`.env` · `data/master_cv.json`, `data/llm_config.json` (API keys), `data/profile_summaries.json`, `data/job_tracker.json` and anything else under `data/` except `data/examples/` · `output/` · `web/dist/` · generated schemas by hand

Never print or log CV contents, API keys, or email bodies beyond what the task needs.

## Shell Command Policy

| Category | Examples |
| --- | --- |
| Run freely | `grep`, `find`, `ls`, `git status`, `git diff`, `git log`, `pytest`, `ruff`, `mypy` |
| Run and report | `python -m src.jobs.fetcher fetch` (hits live APIs, uses quota), `python -m src.services.company_discovery` (crawls ~1,000 sites, writes `data/companies.json`; normally run from the app) |
| Always ask first | `git commit`, `git push`, `pip install`, `conda install`, `npm install <pkg>`, anything calling the paid LLM in a loop |
| Never run | `rm -rf`, `sudo`, `git push --force`, `git reset --hard` |

## Safety Checks (before finalizing any edit)

1. Does the change break existing function signatures, CLI flags, or the JSON schemas?
2. Does it introduce new dependencies? If yes, ask first.
3. Does it touch a protected path, the no-fabrication guards, or source compliance? If yes, stop and ask.
4. Do `pytest`, `ruff check`, and `mypy src` pass?

## Session Startup

1. Run `git status` to see current state.
2. Identify the relevant skill doc and files.
3. State the plan in 2–3 sentences before editing.

## Auto-Compaction Preservation

When context compacts, preserve: files being edited, error tracebacks, architecture decisions,
task description, user constraints. Drop: general conversation, acknowledgements, completed steps.
