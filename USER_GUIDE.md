# User guide: job search and CV writing

This guide is for the local web app at **http://localhost:5173**. It covers setup, model access, Gmail job alerts, searching, and Word documents. You can use the buttons in the app for normal work; the terminal steps below are only for initial setup.

The app is free for personal job searching under its [noncommercial license](LICENSE.md). AI
providers or job source APIs may charge separately. Commercial use requires a separate paid
license from the copyright holder before use.

### Ask a coding agent to set it up

If you use a coding agent such as Codex, Claude Code, Cursor, or a VS Code agent, open this
repository in the agent and ask:

> Set up this job search app on my computer for local use. Read AGENTS.md, CLAUDE.md, and
> USER_GUIDE.md first. Check whether Miniconda, Node.js 20+, and npm are installed; explain
> anything I need to install or sign in to. Run the repository's setup and verification steps,
> start the app with `bash scripts/dev.sh`, and tell me the local URL. Help me choose a model
> provider in Settings. Keep my CVs, API keys, Gmail credentials, and generated documents in
> local ignored files; do not commit, upload, or print them.

Give the agent access to this repository and answer its installation or provider sign-in
questions. Enter API keys in the app's Settings or your local `.env` file yourself. The agent
can verify the demo search before you add a CV or credentials. This app is intended to run on
your own computer; do not expose its unauthenticated backend or frontend to the internet.

## 1. Start the app

1. Install [Miniconda](https://www.anaconda.com/docs/getting-started/miniconda/main) if `conda` is not available. Install a recent Node.js version with npm if `npm` is not available.
2. Open a terminal in this repository's folder and run:

   ```bash
   bash scripts/dev.sh
   ```

   The script creates the `job_search` Conda environment if needed, installs missing app dependencies, and starts the backend and frontend. The first run can take a few minutes.
3. Open **http://localhost:5173**. Keep the terminal running while you use the app. Run the same command again to restart it. Press **Ctrl+C** to stop it, or run `bash scripts/dev.sh --stop` from the repository folder. If startup fails, check `.run/logs/backend.log` and `.run/logs/frontend.log`.

The app stores your CVs, model settings, Gmail tokens, saved jobs, and generated documents locally. The `data/` and `output/` folders contain private information. Do not share those folders or put API keys in a screenshot, issue, or commit.

## 2. Choose how the app uses AI

Open **Settings** with the gear icon at the top right. The app needs a **quality model** and a **screening model**. The quality model reads CVs, builds profiles, reviews borderline matches, writes documents, and powers the assistant. The screening model judges the first pass of many jobs, so it usually accounts for more calls. Choose both models in Settings, leave **Effort** at *medium* to begin where offered, and click **Save**.

| Settings choice | What you need | How usage is charged or limited |
| --- | --- | --- |
| **Claude** | A Claude Console **API key** | API usage is billed through Claude Console. A Claude Pro/Max subscription does not supply API access. |
| **Claude Code (Pro/Max)** | Claude Code CLI signed in to a supported **Claude plan** | Uses the plan's Claude Code allowance. It does not need an API key in this app. |
| **OpenAI API** | An OpenAI Platform **API key** | API usage is billed separately from ChatGPT subscriptions. |
| **Codex (ChatGPT)** | Codex CLI signed in with a **ChatGPT account** | Uses the account's available Codex allowance and limits, which depend on its plan. It does not need an API key in this app. |
| **OpenRouter** | — | The option is visible, but its capabilities are not ready for this guide. Do not rely on it for a working setup yet. |

**Choose one route.** A paid chat plan and an API account are different products. In particular, choose **Claude Code** to use a Claude plan or **Codex** to use a ChatGPT plan. Choose the **Claude** or **OpenAI API** option only if you intend to use an API key and its separate billing. See [Claude's subscription/API explanation](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console), [OpenAI API quickstart](https://platform.openai.com/docs/quickstart), and [Codex authentication](https://learn.chatgpt.com/docs/auth).

### A. Claude API key

1. Create or sign in to a [Claude Console](https://console.anthropic.com/) account. Set up Console API billing or credits as prompted; your Claude chat subscription is separate.
2. In Claude Console, open **Settings → API keys**, create a key, and copy it. [Claude's API key instructions](https://platform.claude.com/docs/en/manage-claude/authentication).
3. In this app, open **Settings → Claude**, paste the key into **API key**, choose both models, and click **Save**. A saved key appears as a masked placeholder next time; leaving that field blank keeps it.
4. As an alternative, set `ANTHROPIC_API_KEY` in a local `.env` file and restart the app. Do not put a real key in `.env.example`.

### B. Claude Code with a Claude plan

1. Check that your Claude plan includes Claude Code. Install the [Claude Code CLI](https://code.claude.com/docs/en/setup) on the same computer that runs this app.
2. In the app, choose **Settings → Claude Code (Pro/Max)**. If the CLI is installed but not signed in, click **Continue with Claude** and finish the browser sign-in with your plan account. You can also sign in by running `claude` in a terminal.
3. Wait until Settings says it is signed in with your Claude plan. Choose the two models and click **Save**. If it reports an API account instead, sign out of the CLI and sign in with your Claude plan account.
4. The app removes inherited Anthropic API key variables from the Claude Code process so its calls use the CLI sign-in. Outside the app, Claude Code can prioritise `ANTHROPIC_API_KEY` over a subscription; [Anthropic explains the billing difference](https://support.claude.com/en/articles/12304248-manage-api-key-environment-variables-in-claude-code). Avoid setting that variable globally when you intend to use the plan.

### C. OpenAI API key

1. Create or sign in to an [OpenAI Platform](https://platform.openai.com/) account. Set up API billing or credits in Platform. A ChatGPT subscription does not automatically pay for Platform API calls.
2. Create an API key in the [Platform dashboard](https://platform.openai.com/api-keys). [OpenAI API quickstart](https://platform.openai.com/docs/quickstart).
3. In this app, choose **Settings → OpenAI API**, paste the key, choose the quality and screening models available to your account, and click **Save**. The key is stored in the local, ignored `data/llm_config.json` file.
4. Alternatively, set `JOBSEARCH_OPENAI_API_KEY` or `OPENAI_API_KEY` in your local `.env` file and restart the app. If the model list cannot load, Settings lets you type an exact model ID; use one available to your API account.

### D. Codex with a ChatGPT account

1. Install the [Codex CLI](https://learn.chatgpt.com/docs/quickstart) on the computer running this app. The app can also find the CLI included with some Codex editor installations.
2. Choose **Settings → Codex (ChatGPT)**. Click **Continue with ChatGPT** if prompted, finish the browser sign-in, and wait for **Connected to ChatGPT**. You can also run `codex login` in a terminal and choose ChatGPT sign-in.
3. Select the quality and screening models listed for your account, then click **Save**. The app runs the local signed-in CLI; it does not ask you for an OpenAI API key on this route. Check the assistant's usage panel for the allowance reported by your account. [Official Codex authentication guide](https://learn.chatgpt.com/docs/auth).

**Model choice and cost:** Begin with the **Recommended setup** shown under the model pickers. A stronger quality model can help CV reading and document review; a faster screening model matters more to search time and API cost or plan usage. Availability and prices change, so use the model list and your provider's billing/usage page instead of assuming a model ID or fixed price. Changing models does not rewrite an existing profile: open **Profiles** and choose **Update** if you want it rebuilt. A model comparison uses real model calls and should be run deliberately.

If Settings cannot list models, first check the selected provider, key or CLI sign-in, then your account's model access. If a search says **pre-filter only**, check that a CV is selected, **Match against my CV** and **Smart match** are on, and Settings shows an available model provider.

## 3. Connect a Gmail inbox for job alerts

Gmail is optional. The app reads a connected inbox only when you run a search that includes **Gmail alerts**. It requests Google's `gmail.readonly` scope and cannot send or delete mail through that permission. The scope can read the *whole mailbox*, so a separate Gmail account containing only job alerts is a good choice. The app verifies that the signed-in address matches the one you configured. It keeps OAuth tokens and parsed job postings in ignored local `data/` files; it does not save raw email bodies.

Google currently says standard Gmail API usage has **no additional cost below its daily threshold**. Google has announced possible charges above quota thresholds later in 2026, so check [current Gmail API quotas and pricing](https://developers.google.com/workspace/gmail/api/reference/quota) before relying on a permanent free allowance. Model calls used to screen alert jobs may still cost API money or use your plan allowance.

### Google Cloud setup, step by step

1. Create or choose a project in the [Google Cloud Console](https://console.cloud.google.com/). Make sure you are working in that project throughout these steps.
2. Open **APIs & Services → Library**, find **Gmail API**, and click **Enable**. [Google's Gmail setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python).
3. Open **Google Auth Platform**. In **Branding**, give the app a name such as “My Job Alerts” and enter your support/contact email. In **Audience**, choose **External** for a personal Gmail account. If the project belongs to a Google Workspace organisation and only its members will use it, **Internal** may be appropriate instead.
4. If the app is in **Testing** status, add the exact Gmail address you intend to connect under **Audience → Test users**. In **Data Access**, add only `https://www.googleapis.com/auth/gmail.readonly` if the console asks you to select scopes. Do not select mail modification or sending scopes. Google's [OAuth audience guide](https://support.google.com/cloud/answer/15549945?hl=en) explains test users and testing limits.
5. Open **Google Auth Platform → Clients** (or **APIs & Services → Credentials**), choose **Create client**, and select **Web application**. Add this **Authorized redirect URI** exactly:

   ```text
   http://localhost:8000/api/gmail/callback
   ```

   The backend receives the OAuth callback on port **8000**; the app's page on port **5173** is not the redirect URI. Keep `localhost`, `http`, the port, and the path exactly the same. A JavaScript origin is not needed for this server-side flow. [Google's web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server).
6. Copy the new **Client ID** and **Client secret**. In this repository's root, make a local `.env` file by copying `.env.example` if you do not have one. Open `.env` in a text editor and set:

   ```dotenv
   JOBSEARCH_GMAIL_ACCOUNT=your-alerts@gmail.com
   JOBSEARCH_GMAIL_CLIENT_ID=your-client-id
   JOBSEARCH_GMAIL_CLIENT_SECRET=your-client-secret
   ```

   Do not put quotation marks around these example values unless the actual value needs them. Keep `.env` private; it is ignored by Git.
7. Restart with `bash scripts/dev.sh`. In the left panel, expand **Job alerts** and click **Connect Gmail alerts**. The sign-in opens in a new tab. Select the same address as `JOBSEARCH_GMAIL_ACCOUNT`, grant the read-only access, and return to the app. A project in Testing status may show Google's unverified-app warning; proceed only if it is the Cloud project you created and the requested permission is the read-only Gmail scope. The app should show **Connected: your-alerts@gmail.com**.
8. Select a CV, turn on **Match against my CV** and **Smart match**, and make sure **Sources → Gmail alerts** is on. Set the **Date posted** window you want and click **Search**. A normal search includes alert postings within the filters and date window; jobs judged before can reappear using their remembered verdicts. Gmail alerts are skipped if no CV is selected or Smart match is off.

The app parses supported job links in alert emails, particularly LinkedIn and Indeed, rather than displaying every email message. Create job alerts on those sites and have them delivered to the connected inbox. The first Gmail search reads the inbox and caches parsed postings; later searches parse newly encountered messages and reuse the cache. **Reconnect** in Job alerts if access expires. External apps left in Google's **Testing** status can have authorisations, including refresh tokens, expire after seven days; the practical fix is to reconnect. [Google's testing guidance](https://support.google.com/cloud/answer/15549945?hl=en).

If Google reports `redirect_uri_mismatch`, compare the exact URI in step 5 with `JOBSEARCH_GMAIL_REDIRECT_URI` in `.env` (if you changed its default), then restart. If Google says the app is unavailable to you, verify the correct account is a test user. If the app reports a different connected address, change `JOBSEARCH_GMAIL_ACCOUNT` or sign in with the configured address and reconnect.

## 4. Prepare and edit your CV profile

1. In the left **Profile** panel, drop a CV into the upload box or browse for a **PDF, DOCX, MD, or TXT** file up to **10 MB**. Upload stores the original file unchanged. Click its name under **Available CVs** to select it; you can keep several versions.
2. Click **Build profile from selected CV** under **Available profiles**. The quality model reads the CV on this first CV-powered action and builds a general profile. The profile opens for editing. If it already exists, the button opens **Edit general profile** instead of charging for a rebuild.
3. Check the headline, seniority, experience, skills and evidence, target roles, role families, and **Not a fit** list. Correct errors and click **Save profile**. A profile describes what the CV supports; do not add untrue qualifications or achievements.
4. Open **Profiles → Career intent** to record roles or sectors you want to explore, work you prefer or avoid, and eligibility. Click **Save intent**. Career intent describes what you want; it is not proof of experience. If the profile says **intent changed** or **CV changed**, review it and click **Update** to rebuild from the current information. **Pin for searches** forces a chosen stored profile to be used; a newly built general profile is pinned by the build button.
5. To add a genuine fact missing from the CV, use **Profiles → Add evidence**: upload a supporting document or paste its text, then review each proposed fact and click **Add to CV** only when correct. You can also describe a fact to the right-hand assistant; it proposes facts for your approval. Accepted facts update the structured CV. Update the profile afterwards if you want the new evidence reflected there.

To edit the selected CV, click it in the library and use **Open in Word**. Changes saved to its working file in `output/cvs/` appear in the app viewer. To use a corrected document as a new source CV for matching, upload it and select it. **Export CV** makes a general Word copy for manual review; a copy downloaded elsewhere does not sync back to the app. Do not delete an older CV until you are sure you no longer need its associated profiles.

## 5. Choose job sources and company sites

In the left **Search** panel, **Sources** has three rows: **Job boards**, **Company career sites**, and **Gmail alerts**. The switch at the right turns that category on or off. Click the category name to open its source list and switch individual sources. A greyed-out source shows why it is unavailable, such as a missing API key or Gmail connection. The app requires at least one source on.

Some job boards need credentials in `.env`: Reed (`JOBSEARCH_REED_API_KEY`), CV-Library (`JOBSEARCH_CV_LIBRARY_API_KEY`), and Adzuna (`JOBSEARCH_ADZUNA_APP_ID` plus `JOBSEARCH_ADZUNA_APP_KEY`). Other public sources in the picker need no API key. A link to **Search Indeed UK live** opens Indeed in a browser; it does not import those live results. Indeed alerts delivered to Gmail are the import route.

### Company career sites: what the app already covers

Many employers publish jobs only on their own careers site. The app reads those sites directly, so you see jobs that never reach the job boards. It finds them in three ways:

- **Built-in employers.** About 55 large UK life-science employers are included and ready to search: big pharma (for example AstraZeneca, GSK, Pfizer, MSD, Novartis, Takeda, Bayer, Boehringer Ingelheim, UCB), CROs (IQVIA, ICON, Parexel, Labcorp, Charles River), and tools, diagnostics and genomics companies (Thermo Fisher, Danaher and Abcam, Agilent, Illumina, Oxford Nanopore, Lonza). You do not need to add these.
- **The company directory.** The app checks a UK life-science directory (about 1,000 companies) and finds each company's job board where it has one.
- **Your companies.** Any employer you add yourself (see the steps below).

Some company sites cannot be read: a few block automated reading (Eli Lilly, for example), and some only show jobs after scripts run in a browser (Novo Nordisk, Alnylam). Their jobs still reach you through LinkedIn, the job boards and your Gmail alerts.

On a group's shared careers site, each job keeps its own brand. For example, Danaher's board lists jobs from Abcam, Cytiva and Leica, and each card shows the right company, so your cover letter names the right employer.

### Add a company you care about, step by step

1. In the left **Search** panel, click **Sources → Company career sites**.
2. Under **Your companies**, type the **company name**, then paste a link into **Website or careers page link**. The best link, in order:
   - the page that lists the company's open jobs (often reached from **Careers → Search jobs**), or
   - the company's careers page, or
   - its main website.
3. Click **Add** and wait. The app looks for a readable job list, which can take up to a minute. When it succeeds, the company appears under **Your companies** and is searched from now on.
4. If the app says **No readable job list found**, the message says why. What to do:

   | The message says | What it means | What to do |
   | --- | --- | --- |
   | *this is a recruitment agency's talent community* | The link is a sign-up page run by a recruiter, not the company's job list. | Go to the company's own careers page and use its **Search jobs** link instead. |
   | *its jobs are on Avature / Taleo / Eightfold / Salesforce / an older SAP SuccessFactors site, which the app cannot read yet* | The company uses a job system the app does not support. | Nothing to fix on your side. Rely on job boards and alerts for this company. |
   | *the site refuses automated reading (403)* | The website blocks programs like this app. | Open one of the company's job postings in your browser, find the job list it belongs to (the address often contains `myworkdayjobs.com`, `greenhouse.io`, `lever.co`, `jobs.jobvite.com` or `oraclecloud.com`), and paste that address instead. |
   | *careers pages have no readable job feed* or *no careers link on the homepage* | The app could not find a job list from the link you gave. | Try the page that lists the company's jobs, as in step 2. |

5. To remove a company you added, click the **×** beside its name.

**Built-in tip:** if you type the name of a built-in employer (for example *Lonza*), the app uses its known job board, whatever link you paste.

### Find directory job boards and switch boards off

1. The first time you search with **Company career sites** on, the app checks the directory (about ten minutes, only once). You can also start it with **Find job boards now**. **Stop** keeps what was found; the next search continues where it stopped.
2. Later, **Update job boards** re-checks companies not checked yet and results older than 30 days.
3. To leave out boards you do not want, open **Sources → Company career sites**, use **Filter companies** to find them and switch them off. **Shown off** turns off just the filtered boards, **All off** turns off every listed board and **All on** restores them. These switches only affect which boards are searched.
4. A directory company cannot be removed, because the next update would find it again. Switch its board off instead. If several employers share one board, the switch affects all of them.

Public career sites do not always have a readable job list, and a site may be blocked or have no relevant jobs. The results panel reports which sources ran, were skipped or failed. The app reads public pages only within each site's published rules (`robots.txt`). It never signs in to a site or gets around a block.

## 6. Run a useful search

1. Select the CV and review its profile first. Keep **Match against my CV** and **Smart match (AI screening)** on for AI fit scores. If no model is ready, the app can only show deterministic pre-filter scores.
2. Start with a focused **Country**, one or two **Cities**, a sensible **Radius**, and a **Date posted** window such as *Last week*. Leave Country or Cities broad if you are open to remote or relocation roles. Undated saved postings are kept.
3. Choose the sources you want. For a quick first run, turn off company career sites until their boards have been found, and select a small number of sources. For a wider run, add more sources and **Widen to adjacent roles** if you genuinely want nearby career paths. With no titles entered in the current UI, job boards use roles from the selected CV profile.
4. Set **Match threshold**. The default is **60**. Raising it narrows the displayed matches; lowering it includes more possibilities. The AI job matcher still decides whether a job is a real match. Roles below the threshold and hard exclusions have their own tabs.
5. Click **Search**. Watch the source counts and progress. **Stop** keeps work completed so far. If some shortlisted jobs remain unscreened, click **Continue** in the results panel. A completed search appears under **Recent searches** so you can reopen it.
6. In **Search results**, look at **Matches**, **To check**, **Not selected** and **Excluded**. Open a job card's **Details** for fit reasons, gaps and flags. Check the job on the original site with **View on…** before applying. You can mark each job *Would apply*, *Maybe* or *No* under **Your call**. This builds a private set of examples for comparing model setups and does not change that search.

### Read the whole job description

The job card's **Details** show the first lines of the posting. Click **Show full description** to read all of it, including responsibilities and requirements, and **Show less** to fold it again.

When a site only gave a short snippet, the app opens the full posting itself before judging the job. Jobs whose full text could still not be read are listed under **To check**. For those:

1. Click **View on…** to open the posting in your browser and copy its whole text.
2. On the job card, click **Paste description**, paste the text and click **Judge with this description**.

### The app remembers where you were

- **Reloading the page, or opening the app in another tab, brings back the last search you had open**, with its results. If you never opened one, the newest search is shown.
- Click **Clear results** (or **Clear filters and results**) when you want a clean screen. The app then stops reopening a search until you run or open another one. The search itself stays under **Recent searches**.
- To go back to an older search, click it under **Recent searches**. The app keeps your last 10 searches.
- On a small screen, the app also remembers which panel you had open (search, results or assistant).

**Make runs more efficient:** build and correct the profile once before searching; reuse a selected CV and unchanged profile so the app can reuse summaries and past job verdicts. Start with a recent date window and relevant sources. Avoid repeated first-time company discovery during time-sensitive searches; let it finish once. Use a lighter screening model if its results are good enough, and reserve a stronger quality model for CV reading, second opinions, and writing. Changing the profile, intent, or model may cause jobs to be judged again. The app's usage panel shows model usage and, where available, plan limits. Do not run `model_compare` casually: it re-screens labelled jobs and uses model calls.

## 7. Save jobs, track applications, and write documents

The results panel has tabs for **Search results**, **Saved**, **Applied**, **Documents** and **Cover letters**.

### Save jobs and remove them, step by step

1. In **Search results**, tick the box at the left of each job you want to keep.
2. Click **Save** in the bar that appears at the top. The jobs move to **Saved**: they leave the search results (a note shows how many) and stay in **Saved** across new searches.
3. To remove one saved job, open **Saved** and click the red **Delete** button at the top right of its card.
4. To remove several, tick them in **Saved** and click **Remove N from saved** in the top bar.

Deleting a saved job keeps its status, note and any application record. If that job is still in the search on screen, it goes back to the search results.

### Record an application, step by step

1. On the job's card (in results or in **Saved**), set the status to **Applied**. You can add a note and later record the outcome stage (for example an interview).
2. The job moves to the **Applied** tab, which is your record of applications. It leaves **Saved**, and later searches set it aside for the look-back period.
3. For an application made outside the app, open **Applications & sources** at the bottom of the left panel, fill in the title and company, and click **Add application**. It appears in the **Applied** tab.
4. If you marked a job Applied by mistake, click **Move to search** in the **Applied** tab. It returns to an open search when that search contained the job; otherwise it can appear in a future search. An entry added manually has **Clear Applied** instead.

### Keep every list in step

The app keeps all its lists consistent automatically:

- **Applying, saving or marking a job N/A anywhere** (a job card, **Applications & sources**, the **Applied** tab or the assistant) updates **Saved**, **Applied**, **Recent searches** and the results on screen.
- **Deleting a job from the search results** (the red **Delete** in its card's action row) removes it from the results, from **Saved**, and from every search under **Recent searches**, so it does not come back when you reopen an older search.
- **The Applied register is never changed by deleting a job from a list.** Your applications, their stages and notes are always kept.
- **Other open tabs of the app update immediately.** A tab that was in the background also refreshes as soon as you return to it.

### Tailor your CV to a job, step by step

1. Open the job's card. Under the job, choose the options (the defaults suit most jobs):
   - **Design:** *same as my CV* keeps your Word CV's own layout, fonts and sections. *Classic*, *Modern* or *Compact* use a new layout instead.
   - **Length:** *automatic* keeps every line unless the job is clearly more junior than your experience. *Keep full length* never trims, and *Junior role (trim)* shortens it.
   - **Emphasis** (*Leadership* or *Hands-on / wet lab*) and **Level** (*More senior* or *More junior emphasis*) change what leads the CV. They never change your actual titles or experience.
2. Click **Tailor CV** and wait for the progress steps to finish.
3. Click **Download .docx** and read every line before you send it.
   - The CV keeps your own headline. Up to four job-specific headline suggestions are written under it, for you to delete the ones you do not want. You can also pick one in the app, which saves a new version with only that headline.
   - Nothing is removed (unless you chose a trim). The most relevant lines lead each role and section, and the rest follow.
   - The app only rephrases and reorders what your CV already says. It never adds facts, numbers or skills you do not have.
4. Read the **Report** under the job. It shows the keyword coverage before and after, job keywords not found word for word, the review of the first draft, changes that were rejected and why, and any ATS warnings. A missing requirement should be handled truthfully, never by inventing a fact. If you do have that experience, add it to your CV first (see section 4).

The report is kept with the tailored CV. When you come back later, reload the page or open the job in another tab, the card shows it again with the date it was tailored.

### Write a cover letter, step by step

1. On the job's card, click **Cover letter / review CV**.
2. Choose the source: your selected CV or a saved tailored CV for this job. If you import an older Word CV for the job, review and save its text first.
3. Click **Write cover letter** and download it. The letter draws achievements from the CV and motivation from your saved career intent.
4. To edit it in the app, open the **Cover letters** tab, change the greeting, paragraphs or closing, click **Save edits**, then export to Word or text.

To revise a saved tailored CV in the app, use **Review and edit CV** in that job's document panel, then **Save reviewed CV**. Edits saved directly to its file in `output/cvs/` update the document shown in the app. Copies downloaded to another folder do not sync back.

To prepare several jobs at once, tick them and choose **Tailor CV + cover letter** in the top bar, then review each document separately. Saved jobs from older searches can still be used.

### The assistant

The right-hand **Assistant** does things for you from plain-language requests. For example, it can run or refine a search, save, track or remove jobs, tailor a CV or write a letter, edit your profile, career intent or a saved document, and add a company. It changes only what you ask for. New facts about your experience are always proposed for your approval before they reach your CV. Every list updates when the assistant finishes.

## 8. If something does not work

| Symptom | Check |
| --- | --- |
| **Backend unreachable** | Keep `bash scripts/dev.sh` running; open `http://localhost:5173`; inspect `.run/logs/`. |
| **No AI scores / pre-filter only** | Select a CV, enable **Match against my CV** and **Smart match**, then check Settings shows both models and a working key or CLI sign-in. |
| **No profiles yet** | Click **Build profile from selected CV**. If it fails, check the quality model and provider credentials in Settings. |
| **No jobs** | Open the source report and notices; widen the date or location, enable relevant sources, and inspect excluded jobs. A newly added company may have no public feed. |
| **Gmail source unavailable** | Check the three `JOBSEARCH_GMAIL_*` values in `.env`, restart, then connect or reconnect under **Job alerts**. Keep a CV selected and Smart match on. |
| **Google redirect error** | The authorised redirect URI must be exactly `http://localhost:8000/api/gmail/callback`, unless you deliberately changed the backend redirect setting in `.env`. |
| **Plan sign-in fails** | Confirm the CLI is installed on the app's computer and signed in with the intended **plan** account. Use the API provider choice only when you intend API billing. |
| **Job description looks cut off** | Open **Details** and click **Show full description**. If the source only gave a snippet, use **Paste description** on the card (section 6). |
| **A company cannot be added** | Read the reason in the message and follow the table in section 5. The job-list link from one of the company's postings usually works. |
| **A list looks out of date** | Lists update by themselves after every change. If one still looks wrong, reload the page; your last search reopens. |
| **The last search did not reopen** | You may have clicked **Clear results**, or the search was deleted or pushed out of the last 10. Open one from **Recent searches**. |
| **The tailoring report is missing** | The report belongs to CVs tailored in the app. A Word CV imported for a job has no report. |

### Official setup references

- [OpenAI API quickstart](https://platform.openai.com/docs/quickstart) and [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Claude API authentication](https://platform.claude.com/docs/en/manage-claude/authentication) and [Claude Code setup](https://code.claude.com/docs/en/setup)
- [Google Gmail API setup](https://developers.google.com/workspace/gmail/api/quickstart/python), [web application OAuth](https://developers.google.com/identity/protocols/oauth2/web-server), and [Gmail API quotas](https://developers.google.com/workspace/gmail/api/reference/quota)
