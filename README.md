# AI Job Search and CV Tailor

A local web app for finding jobs, judging their fit against your CV, tracking applications,
and preparing tailored Word CVs and cover letters. You can use its buttons or ask the
assistant to help with searches and records. Tailored documents use facts from your CV;
review every document before sending it.

## Get started

Install [Miniconda](https://www.anaconda.com/docs/getting-started/miniconda/main) and
[Node.js](https://nodejs.org/) 20+ with npm (on a Mac, also run `xcode-select --install`), then:

```bash
git clone https://github.com/rgrandym/job_search.git && cd job_search && bash scripts/install.sh
```

The installer sets up the local environment, builds the app and, on a Mac, adds **Job Search**
to `~/Applications` and opens it. Right-click its Dock icon and choose **Options → Keep in
Dock**. Clicking the icon opens the app, or brings its window back if it is already open.
Without the Mac app, run `bash scripts/start.sh` and open
[http://localhost:8000](http://localhost:8000). To update, run `git pull`; the app rebuilds
what changed the next time it starts. Choose an AI provider in **Settings**,
upload a CV, then run a search. You can try the **demo** source before adding a CV or API key.

**Read the [user guide](USER_GUIDE.md)** for full setup instructions, model and account
options, job sources, Gmail alerts, profile editing, searches, application tracking, CV
tailoring, cover letters, and troubleshooting. It also includes a prompt you can give a
coding agent to help install the app.

## Privacy and cost

The app runs on your computer. Your CVs, job records, credentials, and generated documents
belong in the Git-ignored `data/` and `output/` folders. Model providers and some job
sources may charge separately or apply usage limits; see the guide before connecting them.
Do not expose the local app to the internet.

## For contributors and coding agents

Read [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) before changing the app.

## License

Available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md). Personal job
searching and other noncommercial use are free. Commercial use requires a separate license.
