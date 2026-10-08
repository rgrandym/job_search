# AI Job Search and CV Tailor

A local web app for finding jobs, judging their fit against your CV, tracking applications,
and preparing tailored Word CVs and cover letters. You can use its buttons or ask the
assistant to help with searches and records. Tailored documents use facts from your CV;
review every document before sending it.

## Get started

Install [Miniconda](https://www.anaconda.com/docs/getting-started/miniconda/main) and
[Node.js](https://nodejs.org/) with npm, then run this from the repository folder:

```bash
bash scripts/dev.sh
```

The script sets up the local environment and opens the app at
[http://localhost:5173](http://localhost:5173). Choose an AI provider in **Settings**,
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
