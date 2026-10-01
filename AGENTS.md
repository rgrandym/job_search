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
gaps, and tailor-to-.docx. **right**: agent chat over `/api/ws/chat`.
The LLM provider (Claude, OpenAI/Codex, OpenRouter) and the orchestrator and subagent models
are chosen in Settings.

## Agent team

| Agent | Role | Tools | Defined in |
| --- | --- | --- | --- |
| **orchestrator** | Decides what to do. Builds or recalls the profile summary, delegates, reports. | `get_workspace_state`, `summarize_profile`, `delegate` | `src/agents/definitions.py`, `prompts/orchestrator.md` |
| **job_search_expert** | Runs searches, refines strategy once, reports true matches | `search_jobs`, `get_results`, `get_job`, `list_sources` | + skill `job_search` |
| **job_matcher** | Semantic screening of the shortlist against the profile summary | (invoked inside `search_jobs`) | `src/jobs/screener.py` |
| **cv_expert** | Tailors the CV to a chosen job, exports .docx | `get_master_cv`, `tailor_cv`, `update_master_cv`, `get_job` | + skill `cv_writer` |

One bounded loop (`src/agents/runtime.py`) drives every chat agent. `delegate` runs a subagent in a
fresh conversation and returns its answer. Tools are thin wrappers over `src/services/`, so
the Search button, the REST API and the agents execute the same code.

## Data flow 1: Search & match (the critical path)

```text
UI filters / agent overrides ─▶ SearchQuery
        │
        ▼  services/search_service.run_search
1 capture     sources: Reed · CV-Library (1 query per title, radius, salary) · company ATS feeds
              · LinkedIn/Indeed alert emails & saved postings · demo           → JobPosting[]
2 pre-filter  scorer.hard_exclusions (arrangement, location, salary floor, certs, seniority)
              + pre-filter score (skills from lists or extracted from text)    → shortlist (40)
3 enrich      full descriptions for snippet-only shortlisted postings (Reed)
4 summary     orchestrator ProfileSummary  ◀── ProfileMemory (data/profile_summaries.json)
              key = CV fingerprint + role family → same summary for the same type of search
5 screen      job_matcher: batches of 6, concurrent → JobVerdict {match, fit 0-100, reasons, gaps}
        │
        ▼
MatchReport {matches (verdict.match & fit ≥ threshold), below_threshold, excluded+reasons, summary}
        → UI results panel (REST response or `search_results` WebSocket event)
```

No LLM configured → steps 4–5 are skipped and the report says it shows pre-filter scores only.

## Data flow 2: Master CV → Tailoring → Word document

```text
upload (.pdf/.docx/.md/.txt) ─▶ data/cvs/ (stored unchanged; no parsing or LLM call)
select CV + request a CV-powered action ─pypdf/python-docx─▶ text ─LLM─▶ MasterCV
job from results ─▶ tailor.analyze_jd ─▶ propose_plan (LLM) ─▶ apply_plan (guards: source ids,
no new numbers, no unevidenced skills) ─▶ TailoredCV ─▶ export_docx ─▶ output/<Name>_<Company>.docx
```

## Invariants to preserve

- Only `src/core/llm/` and `src/core/llm_provider.py` talk to model APIs.
- Whether a job matches is the job_matcher's semantic verdict, not keyword overlap. Hard
  constraints stay deterministic.
- Tailoring never invents facts. Don't relax `tailor._fabrication_reason`.
- Pydantic models are the source of truth. Regenerate JSON schemas with `export-schema`.
  `web/src/lib/types.ts` mirrors them.
- Never scrape LinkedIn or Indeed. Add new boards as API, ATS-feed or inbox adapters.
- Ask before adding dependencies (pip or npm). Don't commit unless asked.
