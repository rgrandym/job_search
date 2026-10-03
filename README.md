# AI Job Search Engine & Automated CV Tailor

1. **Master CV management**: import a raw text/Markdown CV into a validated, structured JSON Master CV.
2. **Tailored CV generator**: align the Master CV to a job description (ATS keywords, STAR
   bullets) with deterministic anti-fabrication guards, then export to Word (`.docx`).
3. **Job search engine**: capture postings from Reed, CV-Library, Adzuna, LinkedIn/Indeed (alert emails
   and saved postings), and company career sites, then keep only the ones that truly match your
   profile (AI screening against a remembered profile summary).
4. **Web app**: buttons for searching, screening, tailoring, cover letters, saving and tracking
   jobs, plus a small assistant for plain-language updates to your preferences and career intent.
   Runs on Claude (API key or Claude Code through a Pro/Max plan), Codex through a ChatGPT plan,
   the OpenAI API, or OpenRouter models.

## Quick start (web app)

```bash
bash scripts/dev.sh          # creates/activates the conda env, installs deps, (re)starts both servers
```

It opens http://localhost:5173. Running it again restarts both servers cleanly; Ctrl-C (or
`bash scripts/dev.sh --stop`) stops both. Logs are in `.run/logs/`.

1. **Settings** (top right): pick Claude, Claude Code (Pro/Max), Codex (ChatGPT), OpenAI API, or
   OpenRouter, then choose two models, each with its own effort: the **quality** model (profile
   summary, CV reading and tailoring, cover letters, second opinions on matches and near the threshold, the
   assistant) and the **screening** model (first-pass job matching, hundreds of calls per
   search). Claude Code and Codex drive your local, signed-in CLI (`claude -p` / `codex exec`)
   and need no API key; usage counts against your plan's limits. The UI recommends a setup per
   provider (e.g. Sol + Luna, or Sonnet + Haiku); check a change on your own past verdicts with
   `python -m src.services.model_compare --screening-model <id> --sample 40` (it calls the
   model for every sampled posting).
2. **Upload your CV** (PDF, DOCX, MD). It's parsed into a structured Master CV.
3. Set **titles, location, radius, salary, arrangement**, then **Search**. The quality model
   builds an evidence-based profile summary (remembered for the same type of search), and the
   **job_matcher** reads each shortlisted posting and keeps only true matches, with reasons
   and gaps. Leave titles empty to search the roles in your CV profile.
   The results panel shows live progress (each source, pre-filter, screening batch by batch),
   and the sources used. **Profiles** (in the sidebar) lets you view, edit, pin and delete the
   stored profiles for the selected CV. The usage panel shows session tokens and, for Claude
   Code / Codex, your plan's limits.
4. **Tailor CV** / **Cover letter** on any job, or tick jobs to save them or prepare both
   documents for each → download .docx files (no invented facts).

Try it without keys: choose the `demo` source and Search (pre-filter scores only).
Single-server mode (built UI): `cd web && npm run build && cd .. && uvicorn src.web.app:app --port 8000`.

## Real use

```bash
# 1. Build your Master CV, then review it for accuracy
python -m src.cv.master_cv_manager import my_cv.md --out data/master_cv.json

# 2. Capture jobs
#    Company career sites: the app finds the job boards of ~1,000 UK life-science companies on
#    the first search that includes them (~10 min, once); "Update job boards" in the sidebar
#    refreshes them. Add your own companies to data/companies.json (see data/examples).
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
| Adzuna | Official UK search API (`JOBSEARCH_ADZUNA_APP_ID` and `JOBSEARCH_ADZUNA_APP_KEY`); [register](https://developer.adzuna.com/) to get both. Results contain description snippets. |
| Company sites | Public ATS feeds (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio, Teamtailor, Pinpoint), Workday, iCIMS and BambooHR careers endpoints within robots.txt, schema.org JSON-LD for other careers pages. The app finds the boards from the BioPharmGuy UK list; add your own companies under the company-sites toggle |
| LinkedIn | Public (logged-out) job search: one search per title, a few pages, a request every 2.5 s; full postings fetched only for the shortlist. Stops quietly if LinkedIn asks it to slow down. |
| Totaljobs | Public search pages (first page per title and place, within robots.txt); judged from snippets. |
| jobs.ac.uk | Public search (UK-wide; universities, institutes, spinouts), with closing dates. |
| NHS Jobs | Public XML search API, with closing dates. |
| LinkedIn, Indeed alerts | Your job-alert emails + postings you save. Indeed blocks plain HTTP, so it comes in through alerts (Gmail) only; the sidebar also opens a live Indeed UK search in a new tab. |
| Gmail job alerts | Manual read-only sync of a dedicated Gmail inbox; new Indeed and LinkedIn alert jobs are matched once per selected CV. |

### Connect a dedicated Gmail alert inbox

1. In a Google Cloud project, enable the Gmail API. Set up an **External** OAuth consent screen,
   add the dedicated Gmail account as a test user, and request only
   `https://www.googleapis.com/auth/gmail.readonly`.
2. Create an OAuth client of type **Web application**. Add
   `http://localhost:8000/api/gmail/callback` as an authorised redirect URI.
3. In the local, ignored `.env`, set `JOBSEARCH_GMAIL_ACCOUNT`, `JOBSEARCH_GMAIL_CLIENT_ID`,
   and `JOBSEARCH_GMAIL_CLIENT_SECRET`. Restart the app, then click **Connect Gmail alerts**.
   Sign in with the dedicated account; the app verifies its address before storing the token.
4. Select a CV and click **Search new alerts for this CV**. This is the only action that fetches
   Gmail messages. The first search considers all current inbox alerts; later searches analyse only
   postings not yet considered with that CV. A different CV gets its own history.

The app stores OAuth tokens, parsed alert postings, and per-CV history in ignored `data/` files
with owner-only permissions. It never stores raw email bodies. Gmail's OAuth permission covers
the whole dedicated mailbox, so keep that account limited to job alerts. If Google's consent
screen remains in Testing mode, its refresh token expires after seven days; use **Reconnect**
when access expires. Do not put the OAuth client secret or tokens in a commit or chat message.

## How matching works

1. **Hard constraints** (deterministic): work arrangement, location/radius, salary floor,
   required certifications, seniority gap.
2. **Pre-filter score** (deterministic, 0–100): title 25 · skills 35 · experience 20 · location 10 ·
   context 10. This only builds the shortlist.
3. **Profile summary** (quality model): an evidence-based reading of your CV for the searched
   role family, remembered and reused.
4. **job_matcher** (screening model; second opinions on matches and near the threshold by the quality model):
   judges each shortlisted posting like a recruiter. A fit score at or above the threshold
   (default 60) with `match` set means a true match.

Details: [.agent/skills/job_search/SKILL.md](.agent/skills/job_search/SKILL.md).

## For AI agents

- Claude Code: [CLAUDE.md](CLAUDE.md). Skills are linked into `.claude/skills/`.
- Codex / Cursor / VS Code agents: [AGENTS.md](AGENTS.md)
- Skills: [.agent/skills/](.agent/skills/) · Agent configs: [.agent/agent_configs/](.agent/agent_configs/)

Personal data (`data/*` except `data/examples/`, and `output/`) is git-ignored.
