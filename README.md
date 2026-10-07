# AI Job Search Engine & Automated CV Tailor

This local app helps individuals find relevant job postings, compare them with their CV and
career goals, save and track applications, and create evidence-based tailored CVs and cover
letters. It is free to use for personal job searching. Model providers and some job sources may
charge separately or apply usage limits.

**New user?** Follow the [step-by-step user guide](USER_GUIDE.md) for model providers, Gmail alerts, CVs, searches, company sites, and documents.

1. **Master CV management**: import a raw text/Markdown CV into a validated, structured JSON Master CV.
2. **Tailored CV generator**: align the Master CV to a job description (ATS keywords, STAR
   bullets) with deterministic anti-fabrication guards, then export to Word (`.docx`).
3. **Job search engine**: capture postings from Reed, CV-Library, Adzuna, LinkedIn/Indeed (alert emails
   and saved postings), and company career sites, then keep only the ones that truly match your
   profile (AI screening against a remembered profile summary).
4. **Web app**: buttons for searching, screening, tailoring, cover letters, saving and tracking
   jobs, plus an assistant that can operate searches, documents and records in plain language.
   Runs on Claude (API key or Claude Code through a Pro/Max plan), Codex through a ChatGPT plan,
   or the OpenAI API. The OpenRouter option is not yet ready for general use.

## Quick start (web app)

```bash
bash scripts/dev.sh          # creates/activates the conda env, installs deps, (re)starts both servers
```

It opens http://localhost:5173. Running it again restarts both servers cleanly; Ctrl-C (or
`bash scripts/dev.sh --stop`) stops both. Logs are in `.run/logs/`.

1. **Settings** (top right): pick Claude, Claude Code (Pro/Max), Codex (ChatGPT), or OpenAI API,
   then choose the models, each with its own effort: the **profile** model (the profile
   summary every job is judged against; blank uses the quality model; it may use another signed-in provider, e.g. Opus through Claude Code while Codex screens), the **quality** model (CV reading and tailoring, cover letters, second opinions on matches and near the threshold, the
   assistant) and the **screening** model (first-pass job matching, hundreds of calls per
   search). Claude Code and Codex drive your local, signed-in CLI (`claude -p` / `codex exec`)
   and need no API key; usage counts against your plan's limits. The UI recommends a setup per
   provider (e.g. Sol + Luna, or Sonnet + Haiku); check a change on your own past verdicts with
   `python -m src.services.model_compare --screening-model <id> --sample 40` (it calls the
   model for every sampled posting).
   The assistant's chat picker also offers your signed-in Claude Code models, even when
   another provider is selected for searches. Chat tokens appear in the existing usage panel.
2. **Upload your CV** (PDF, DOCX, MD, TXT). It is stored unchanged and parsed when a CV-powered action needs it.
   Click **Build profile from selected CV** to review and edit the profile before searching.
3. Choose **sources, country, cities, radius, and date posted**, then **Search**. The quality model
   builds or reuses an evidence-based profile summary, and the
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
#    refreshes them. Add your own companies in Sources → Company career sites → Your companies.
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

See [Connect a Gmail inbox for job alerts](USER_GUIDE.md#3-connect-a-gmail-inbox-for-job-alerts) for the full Google Cloud setup. After connecting, select a CV, enable **Match against my CV** and **Smart match**, leave **Gmail alerts** on under Sources, and click **Search**. The search reads the connected inbox on demand.

## How matching works

1. **Hard constraints** (deterministic): work arrangement, location/radius, salary floor,
   required certifications, seniority gap.
2. **Pre-filter score** (deterministic, 0–100): title 25 · skills 35 · experience 20 · location 10 ·
   context 10. This only builds the shortlist.
3. **Profile summary** (profile model, or the quality model when none is set): an evidence-based reading of your CV for the searched
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

## License

This project is available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).
Personal job searching and other noncommercial use are free. Commercial use requires a separate
paid commercial license from the copyright holder before use.
This is a source-available license, not an open-source license.

Before sharing a clone or committing new files, review `git status --short` and
`git ls-files --others --exclude-standard`. Keep CVs, job records, API credentials, and
generated documents in the ignored `data/` and `output/` folders. Ignore rules do not
protect a file that was already tracked or one explicitly added with `git add -f`.
