# AI Job Search Engine & Automated CV Tailor

1. **Master CV management**: import a raw text/Markdown CV into a validated, structured JSON Master CV.
2. **Tailored CV generator**: align the Master CV to a job description (ATS keywords, STAR
   bullets) with deterministic anti-fabrication guards, then export to Word (`.docx`).
3. **Job search engine**: capture postings from Reed, CV-Library, LinkedIn/Indeed (alert emails
   and saved postings), and company career sites, then keep only the ones that truly match your
   profile (AI screening against a remembered profile summary).
4. **Web app + agents**: an orchestrator agent with job-search, job-matching and CV specialists,
   running on Claude, Codex through a ChatGPT plan, the OpenAI API, or OpenRouter models.

## Quick start (web app)

```bash
bash scripts/dev.sh          # creates/activates the conda env, installs deps, (re)starts both servers
```

It opens http://localhost:5173. Running it again restarts both servers cleanly; Ctrl-C (or
`bash scripts/dev.sh --stop`) stops both. Logs are in `.run/logs/`.

1. **Settings** (top right): pick Claude, Codex (ChatGPT), OpenAI API, or OpenRouter, then choose
   the orchestrator and subagent models. Codex uses your local ChatGPT login and needs no API key;
   the other providers use their own API credentials. For Codex/OpenAI, the UI recommends a
   balanced model for profile summaries and orchestration, a faster model for repeated search and
   matching, and a stronger balanced model when CV tailoring quality matters most.
2. **Upload your CV** (PDF, DOCX, MD). It's parsed into a structured Master CV.
3. Set **titles, location, radius, salary, arrangement**, then **Search**, or ask the agent.
   The orchestrator builds an evidence-based profile summary (remembered for the same type of
   search), and the **job_matcher** subagent reads each shortlisted posting and keeps only true
   matches, with reasons and gaps.
4. **Tailor CV** on any match → download a .docx (no invented facts).

Try it without keys: choose the `demo` source and Search (pre-filter scores only).
Single-server mode (built UI): `cd web && npm run build && cd .. && uvicorn src.web.app:app --port 8000`.

## Real use

```bash
# 1. Build your Master CV, then review it for accuracy
python -m src.cv.master_cv_manager import my_cv.md --out data/master_cv.json

# 2. Capture jobs
cp data/examples/companies.example.json data/companies.json   # edit: your target companies
#    put LinkedIn/Indeed alert emails (.eml) in data/inbox/, saved postings in data/inbox/postings/
python -m src.jobs.fetcher fetch --keywords "machine learning" --locations London --out data/jobs.json

# 3. Score
python .agent/skills/job_search/scoring_engine.py --cv data/master_cv.json --jobs data/jobs.json

# 4. Tailor + export for a strong match (see .agent/skills/cv_writer/SKILL.md)
```

| Source | Mechanism |
| --- | --- |
| Reed | Official API (`JOBSEARCH_REED_API_KEY`) |
| CV-Library | Official API, partner key (`JOBSEARCH_CV_LIBRARY_API_KEY`) |
| Company sites | Greenhouse / Lever / Ashby public feeds; schema.org JSON-LD for other careers pages |
| LinkedIn, Indeed | Your job-alert emails + postings you save. These sites forbid scraping and have no public search API. |

## How matching works

1. **Hard constraints** (deterministic): work arrangement, location/radius, salary floor,
   required certifications, seniority gap.
2. **Pre-filter score** (deterministic, 0–100): title 25 · skills 35 · experience 20 · location 10 ·
   context 10. This only builds the shortlist.
3. **Profile summary** (orchestrator): an evidence-based reading of your CV for the searched role
   family, remembered and reused.
4. **job_matcher** (subagent): judges each shortlisted posting like a recruiter. A fit score ≥ 70
   with `match` set means a true match.

Details: [.agent/skills/job_search/SKILL.md](.agent/skills/job_search/SKILL.md).

## For AI agents

- Claude Code: [CLAUDE.md](CLAUDE.md). Skills are linked into `.claude/skills/`.
- Codex / Cursor / VS Code agents: [AGENTS.md](AGENTS.md)
- Skills: [.agent/skills/](.agent/skills/) · Agent configs: [.agent/agent_configs/](.agent/agent_configs/)

Personal data (`data/*` except `data/examples/`, and `output/`) is git-ignored.
