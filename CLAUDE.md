# AI Job Search Engine & CV Tailor: Claude Code Instructions

## Project

**Backend:** Python 3.11+ (env runs 3.12) · FastAPI + WebSocket · Pydantic v2 · pydantic-settings · httpx · python-docx · pypdf
**Frontend (`web/`):** React 18 · TypeScript (strict) · Vite · Tailwind (CSS tokens in `index.css`) · Zustand · TanStack Query · Lucide · react-resizable-panels
**Conda env:** `job_search`. Always activate it before running Python commands.
**LLMs:** Claude (Anthropic SDK), OpenAI or OpenRouter (Chat Completions), selected in the UI. Separate
orchestrator and subagent models. Default `claude-opus-5-5`. All model access goes through `src/core/llm/`.
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
    llm/       types.py · anthropic_backend.py · openai_backend.py (OpenAI + OpenRouter)
  cv/          models.py · master_cv_manager.py · tailor.py · docx_exporter.py
  jobs/        models.py · fetcher.py · matcher.py + scorer.py (pre-filter)
               profile_memory.py (orchestrator summary + memory) · screener.py (job_matcher)
    sources/   base.py (HTTP, robots, JSON-LD) · job_boards.py (Reed, CV-Library)
               companies.py (Greenhouse, Lever, Ashby, careers pages) · inbox.py (LinkedIn/Indeed alerts, saved postings)
  tools/       search_tools.py (pure text utils) · docx_tools.py (python-docx primitives)
  services/    workspace.py (state) · search_service.py (THE search pipeline) · cv_service.py
  agents/      registry.py · runtime.py (loop + delegate) · tools.py · definitions.py · prompts/*.md · chat.py
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
   the job_matcher's semantic verdict** against the orchestrator's profile summary, not keyword
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
9. **Agents.** Add a tool with `@tool(name, description, ArgsModel)` in `src/agents/tools.py` (thin
   wrapper over a service). Add a subagent with `register_agent(...)` in `definitions.py`, a prompt
   in `prompts/`, and the `delegate` Literal. Frontend types in `web/src/lib/types.ts` mirror the
   backend models. Update both together.

## No-Fabrication Contract (MANDATORY)

A CV is a factual document. Tailoring may rephrase and reorder. It may never invent.
- Every tailored bullet carries `source_id` → a Master CV bullet. Unknown ids are rejected.
- `apply_plan` rejects rewrites that add numbers, or add JD skills that the Master CV doesn't evidence.
  **Do not weaken these guards** to make a test or a demo pass. Fix the prompt or the data.
- Every accept/reject is recorded in `TailoredCV.changes` with a reason. Surface rejections and
  `missing_keywords` to the user.
- New facts go into the Master CV (after the user confirms them), never straight into a tailored CV.

## Job Source Compliance (MANDATORY)

- Official APIs (Reed, CV-Library), public ATS feeds (Greenhouse, Lever, Ashby), or JSON-LD
  from careers pages whose `robots.txt` allows us. All HTTP goes through `sources.base.HttpFetcher`
  (User-Agent, per-host delay, robots checks).
- **LinkedIn and Indeed: no scraping, ever.** No logins, no search-page crawling, no headless
  browsers, no proxy rotation, no CAPTCHA or anti-bot circumvention. Capture them only via
  the user's alert emails (`data/inbox/*.eml`) and postings the user saved (`data/inbox/postings/`).
- New source = new adapter in `src/jobs/sources/` implementing `fetch(SearchQuery) -> list[JobPosting]`,
  registered in `fetcher.build_sources`, tested with `httpx.MockTransport`.
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

`.env` · `data/master_cv.json`, `data/llm_config.json` (API keys), `data/profile_summaries.json` and anything else under `data/` except `data/examples/` · `output/` · `web/dist/` · generated schemas by hand

Never print or log CV contents, API keys, or email bodies beyond what the task needs.

## Shell Command Policy

| Category | Examples |
| --- | --- |
| Run freely | `grep`, `find`, `ls`, `git status`, `git diff`, `git log`, `pytest`, `ruff`, `mypy` |
| Run and report | `python -m src.jobs.fetcher fetch` (hits live APIs, uses quota) |
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
