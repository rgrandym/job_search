# User guide: job search and CV writing

This app runs on your own computer. On a Mac it opens as its own window from the Dock. On
Windows or Linux, or on a Mac if you prefer, you use it in **Google Chrome** at
**http://localhost:8000**. Section 1 takes you from nothing to the app open on screen. After that
you work with the buttons in the app; the terminal is only needed to install, start and update.

The app is free for personal job searching under its [noncommercial license](LICENSE.md). AI
providers or job source APIs may charge separately. Commercial use requires a separate paid
license from the copyright holder before use.

## 1. Install and open the app

### What you need first

| Tool | Why | Check it is installed (in a terminal) | Install it |
| --- | --- | --- | --- |
| **Git** | Downloads the app | `git --version` | Mac: run `xcode-select --install` (it includes Git). Windows: use Git inside WSL (below). Linux: your package manager, e.g. `sudo apt install git`. |
| **Miniconda** | Runs the app's Python | `conda --version` | [Miniconda installer](https://www.anaconda.com/docs/getting-started/miniconda/main). Close and reopen the terminal afterwards. |
| **Node.js 20 or newer** | Builds the app's screens | `node --version` (must say v20 or higher) | [nodejs.org](https://nodejs.org/), the LTS version. |
| **Apple command line tools** (Mac only) | Builds the Dock app | `xcode-select -p` (prints a folder) | `xcode-select --install` |
| **Google Chrome** (recommended) | The browser version, and reading job postings that need a browser | open Chrome | [google.com/chrome](https://www.google.com/chrome/) |

You also need a way for the app to use AI (a Claude or ChatGPT plan, or an API key). You choose
it inside the app after installing; see section 2.

### On a Mac: install the Job Search app

1. Open **Terminal** (press **Cmd+Space**, type *Terminal*, press **Return**).
2. Go to the folder where you want the app to live, for example your home folder:

   ```bash
   cd ~
   ```

3. Download the app and install it. Copy these three lines, paste them into Terminal and press
   **Return**:

   ```bash
   git clone https://github.com/rgrandym/job_search.git
   cd job_search
   bash scripts/install.sh
   ```

   The script checks the tools above and says exactly what is missing if something is. Install
   that, open a new Terminal window, `cd ~/job_search`, and run `bash scripts/install.sh` again.
   The first install takes a few minutes. It never touches your CVs, settings or documents, so it
   is safe to run again at any time.
4. When it finishes, **Job Search** opens in its own window and its icon appears in the Dock.
   Right-click the icon and choose **Options → Keep in Dock**.
5. **Every day after that:** click the **Job Search** icon in the Dock. You do not need Terminal.
   Closing the window keeps the app running in the background (click the icon to bring it
   back); **Cmd+Q** quits it. The app is also in your home folder's **Applications** folder.
   Downloads go to your Downloads folder, and job postings and the Gmail sign-in open in your
   normal browser.

If you move the `job_search` folder, rebuild the Dock icon with `bash scripts/macos/make_app.sh`
from inside the folder. If the app does not open, its startup log is `.run/logs/app.log` in the
folder.

### On Windows, Linux or Chrome: use the browser version

The app is the same; it opens as a page in Google Chrome instead of its own window.

1. **Windows only:** install **WSL** (Windows Subsystem for Linux) once. Open **PowerShell** as
   administrator, run `wsl --install`, restart the computer, and open **Ubuntu** from the Start
   menu. Do every step below inside that Ubuntu window, and install Git, Miniconda and Node.js
   there (the Linux instructions in the table above). [Microsoft's WSL guide](https://learn.microsoft.com/windows/wsl/install).
2. In a terminal (Ubuntu on Windows, any terminal on Linux or Mac), download and install the app:

   ```bash
   cd ~
   git clone https://github.com/rgrandym/job_search.git
   cd job_search
   bash scripts/install.sh
   ```

   On a Mac this also makes the Dock app; add `--no-app` (`bash scripts/install.sh --no-app`)
   if you only want the browser version.
3. Start the app:

   ```bash
   bash scripts/start.sh
   ```

4. Open **Google Chrome** and go to **http://localhost:8000**. Bookmark it. To give it its own
   window and icon, use Chrome's menu **⋮ → Cast, save and share → Install page as app**.
5. Keep the terminal open while you use the app. To stop it, press **Ctrl+C** in that terminal.
   **Every day after that:** open a terminal, run `cd ~/job_search` then `bash scripts/start.sh`,
   and open your bookmark.

Other browsers usually work too, but Chrome is the one the app is tested with.

### Update the app

In a terminal, run:

```bash
cd ~/job_search
git pull
```

Then open the app as usual. It rebuilds whatever changed the first time it starts, which can take
a minute. Your CVs, settings, saved jobs and documents are kept.

### Keep your data private

The app stores your CVs, model settings and keys, Gmail sign-in, saved jobs and generated
documents on your computer, in the `data/` and `output/` folders and the `.env` file. They are
never uploaded with the code. Do not share those folders or put API keys in a screenshot, issue
or commit. The app has no login of its own, so run it only on your own computer and do not make
it reachable from the internet.

### Ask a coding agent to set it up (optional)

If you use a coding agent such as Claude Code, Codex, Cursor or a VS Code agent, you can let it
do the steps above. Open the downloaded `job_search` folder in the agent and ask:

> Set up this job search app on my computer for local use. Read AGENTS.md, CLAUDE.md, and
> USER_GUIDE.md first. Check whether Miniconda, Node.js 20+, and npm are installed; explain
> anything I need to install or sign in to. Run the repository's setup and verification steps,
> run `bash scripts/install.sh`, and tell me how to open the app. Help me choose a model
> provider in Settings. Keep my CVs, API keys, Gmail credentials, and generated documents in
> local ignored files; do not commit, upload, or print them.

Answer its questions about installing tools or signing in. Enter API keys in the app's Settings
or your local `.env` file yourself.

### For developers

To work on the app's code, run `bash scripts/dev.sh` instead. It reloads on changes and serves
the page at **http://localhost:5173**. Run it again to restart, press **Ctrl+C** or run
`bash scripts/dev.sh --stop` to stop it, and check `.run/logs/backend.log` and
`.run/logs/frontend.log` if it fails.

## 2. Choose how the app uses AI

Open **Settings** with the gear icon at the top right. A search uses the **quality model** and **screening model** you choose there. The quality model reads a CV when needed, builds a search summary if no saved profile fits, and reviews borderline matches. The screening model judges the first pass of many jobs, so it usually accounts for more calls. Profile building, CV writing and cover letters can each use a separate model and provider; their limits do not stop a Codex search. Leave **Effort** at *medium* to begin where offered, and click **Save**.

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
4. As an alternative, set `ANTHROPIC_API_KEY` in the local `.env` file and restart the app (section 1). Do not put a real key in `.env.example`.

### B. Claude Code with a Claude plan

1. Check that your Claude plan includes Claude Code. Install the [Claude Code CLI](https://code.claude.com/docs/en/setup) on the same computer that runs this app.
2. In the app, choose **Settings → Claude Code (Pro/Max)**. If the CLI is installed but not signed in, click **Continue with Claude** and finish the browser sign-in with your plan account. You can also sign in by running `claude` in a terminal.
3. Wait until Settings says it is signed in with your Claude plan. Choose the two models and click **Save**. If it reports an API account instead, sign out of the CLI and sign in with your Claude plan account.
4. The app removes inherited Anthropic API key variables from the Claude Code process so its calls use the CLI sign-in. Outside the app, Claude Code can prioritise `ANTHROPIC_API_KEY` over a subscription; [Anthropic explains the billing difference](https://support.claude.com/en/articles/12304248-manage-api-key-environment-variables-in-claude-code). Avoid setting that variable globally when you intend to use the plan.
5. The Claude Code CLI keeps each sign-in in the macOS Keychain and renews it every few hours. When the app, VS Code and a terminal all use the same Claude sign-in, they can try to renew it at the same moment, and the ones that lose say they are not signed in. Codex keeps its sign-in in one file and is rarely affected. If you have more than one Claude account in **Settings → Claude accounts**, the app skips an account for 10 minutes after a sign-in error, then tries it again. To make this less likely, give the app its own Claude account folder rather than the one VS Code uses.

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

   The app receives the Google sign-in on port **8000**, also when you use the developer page on port 5173. Keep `localhost`, `http`, the port, and the path exactly the same. A JavaScript origin is not needed for this server-side flow. [Google's web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server).
6. Copy the new **Client ID** and **Client secret**. In the `job_search` folder, make a local `.env` file by copying `.env.example` if you do not have one (`cp .env.example .env`). Open `.env` in a text editor and set:

   ```dotenv
   JOBSEARCH_GMAIL_ACCOUNT=your-alerts@gmail.com
   JOBSEARCH_GMAIL_CLIENT_ID=your-client-id
   JOBSEARCH_GMAIL_CLIENT_SECRET=your-client-secret
   ```

   Do not put quotation marks around these example values unless the actual value needs them. Keep `.env` private; it is ignored by Git.
7. Restart the app so it reads `.env`: quit it with **Cmd+Q** and click the Dock icon again, or stop `bash scripts/start.sh` with **Ctrl+C** and run it again. In the left panel, expand **Job alerts** and click **Connect Gmail alerts**. The sign-in opens in a new tab. Select the same address as `JOBSEARCH_GMAIL_ACCOUNT`, grant the read-only access, and return to the app. A project in Testing status may show Google's unverified-app warning; proceed only if it is the Cloud project you created and the requested permission is the read-only Gmail scope. The app should show **Connected: your-alerts@gmail.com**.
8. Select a CV, turn on **Match against my CV** and **Smart match**, and make sure **Sources → Gmail alerts** is on. Set the **Date posted** window you want and click **Search**. A normal search includes alert postings within the filters and date window; jobs judged before can reappear using their remembered verdicts. Gmail alerts are skipped if no CV is selected or Smart match is off.

The app parses supported job links in alert emails, particularly LinkedIn and Indeed, rather than displaying every email message. Create job alerts on those sites and have them delivered to the connected inbox. The first Gmail search reads the inbox and caches parsed postings; later searches parse newly encountered messages and reuse the cache. **Reconnect** in Job alerts if access expires. External apps left in Google's **Testing** status can have authorisations, including refresh tokens, expire after seven days; the practical fix is to reconnect. [Google's testing guidance](https://support.google.com/cloud/answer/15549945?hl=en).

If Google reports `redirect_uri_mismatch`, compare the exact URI in step 5 with `JOBSEARCH_GMAIL_REDIRECT_URI` in `.env` (if you changed its default), then restart. If Google says the app is unavailable to you, verify the correct account is a test user. If the app reports a different connected address, change `JOBSEARCH_GMAIL_ACCOUNT` or sign in with the configured address and reconnect.

## 4. Prepare and edit your CV profile

1. In the left **Profile** panel, drop a CV into the upload box or browse for a **PDF, DOCX, MD, or TXT** file up to **10 MB**. Upload stores the original file unchanged. Click its name under **Available CVs** to select it; you can keep several versions.
2. Click **Build profile from selected CV** under **Available profiles**. The profile model (or quality model if you left Profile blank) reads the CV and builds a general profile. The profile opens for editing. If it already exists, the button opens **Edit general profile** instead of charging for a rebuild.
3. Check the headline, pitch, seniority and what sets it, experience, skills (with how recent each is) and evidence, target roles, role families (with how to present you for each), **Where the experience could lead**, and the **Not a fit** list. Correct errors and click **Save profile**. A profile describes what the CV supports; do not add untrue qualifications or achievements.
4. Open **Profiles → Career intent** to record roles or sectors you want to explore, work you prefer or avoid, and eligibility. Click **Save intent**. Career intent describes what you want; it is not proof of experience. If the profile says **intent changed** or **CV changed**, review it and click **Update** to rebuild from the current information. **Pin for searches** forces a chosen stored profile to be used; a newly built general profile is pinned by the build button.

   The selected CV and the pinned profile stay as they are until you choose another CV or profile, including after you quit and reopen the app. Opening the selected CV to read it does not unpin its profile; selecting a **different** CV does, because each profile belongs to its own CV.
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
2. Start with a focused **Country**, one or two **Cities**, a sensible **Radius** (5 to 100 miles: 5, 10, 20, 25, then every 10 miles), and a **Date posted** window such as *Last week*. Leave Country or Cities broad if you are open to remote or relocation roles. Undated saved postings are kept.
3. Choose the sources you want. For a quick first run, turn off company career sites until their boards have been found, and select a small number of sources. For a wider run, add more sources and **Widen to adjacent roles** if you genuinely want nearby career paths. With no titles entered in the current UI, job boards use roles from the selected CV profile.
4. Set **Match threshold**. The default is **60**. Raising it narrows the displayed matches; lowering it includes more possibilities. The AI job matcher still decides whether a job is a real match. Roles below the threshold and hard exclusions have their own tabs.
5. Click **Search**. Watch the source counts and progress. Up to three sources are searched at the same time, and shortlisted postings are opened on several sites at once (LinkedIn in its own, slower lane), so a slow site does not hold up the rest. This speeds the search up without using any more AI: these steps make no model calls. **Stop** keeps work completed so far. If some shortlisted jobs remain unscreened, click **Continue** in the results panel. If a source failed, click **Retry** under **Not used**: only the failed sources are searched again, and the new jobs they find are added to the same results. A source is not retried by itself: a failed source is reported at once (LinkedIn is the exception: when it asks the app to slow down, the app waits and tries again a few times). See *A source failed* in section 8 for what each error means. A completed search appears under **Recent searches** so you can reopen it.
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
- Your choices stay as you left them after a reload or restart: the search filters (country, cities, radius, date posted, sources, threshold), the selected CV and pinned profile, the tailoring options on the job cards (design, length, emphasis, level, audience and writing mode, shared by every card), the CV design for preparing documents for ticked jobs, the results list and tab you had open, and the assistant's model.

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

### Arrange the screen and choose a theme

- **Columns:** drag the line between two columns to resize them. Drag a column past its
  narrowest width, or use the three panel buttons at the top right, to collapse it to a slim
  rail; click the rail to open it again. The layout is remembered.
- **Sections** (Profile, Search, Job alerts, Activity, Models & usage, Recent searches and
  others): click the title to fold or unfold, and drag the grip under a section to set its
  height (double-click the grip to fit the content again).
- **Windows** (Settings, Profiles, the CV editor and others): drag any edge or corner to
  resize; double-click an edge for the default size. The minimise button, or a double-click on
  the title, folds the window into a bar at the bottom right so you can keep working.
- **Text size:** job cards, the tailored CV review and the **Compare CV versions** window use larger text than the rest of the app, for easier reading. To make everything larger or smaller, use the **−  100%  +** text-size control in the top bar (click the percentage to return to 100%).
- **Themes:** the palette button at the top right offers fifteen themes in Claude's colours:
  Claude Light (the default) and Dark, Charcoal, Manilla, Kraft, Cloud, Slate, Sage, High
  contrast, and colour-blind-friendly light and dark versions (warm, charcoal grey, and the
  research app's pair, blue for good and orange for problems). Your choice stays until you pick another or choose **Reset to the
  default**. It is kept by the browser you use (the Dock app keeps its own).

### Stop anything that is running

Every long action shows its progress with a **Stop** button: tailoring a CV, writing a cover
letter, building or updating a profile, exporting a CV, preparing documents for several jobs,
finding company job boards, a search, and the assistant. Stop ends it at once: model calls in
progress are cut off, so nothing more is spent. A stopped search keeps the jobs found and
judged so far; stopped company discovery keeps the job boards found so far. A stopped document
is simply not created; start it again when you are ready.

### Tailor your CV to a job, step by step

1. Open the job's card. Under the job, choose the options (the defaults suit most jobs):
   - **Design:** *same as my CV* keeps your Word CV's own layout, fonts and sections. *Classic*, *Modern* or *Compact* use a new layout instead.
   - **Length:** *automatic* keeps every line unless the job is clearly more junior than your experience. *Keep full length* never trims, and *Junior role (trim)* shortens it.
   - **Emphasis** (*Leadership* or *Hands-on / wet lab*) and **Level** (*More senior* or *More junior emphasis*) change what leads the CV. They never change your actual titles or experience. For a more junior job, your hands-on work leads and your senior experience stays further down as extra strength.
   - **Audience:** *automatic* writes for an academic post when the job is one. *Academic* words the summary as a research profile and moves your existing sections into the order academic readers expect (education and publications before experience). Nothing new is added. *Industry* keeps your CV's own order.
2. Click **Tailor CV** and wait for the progress steps to finish.
3. Click **Download .docx** and read every line before you send it.
   - The CV keeps your own headline. Up to four job-specific headline suggestions are written under it, for you to delete the ones you do not want. You can also pick one in the app, which saves a new version with only that headline.
   - Nothing is removed (unless you chose a trim). The most relevant lines lead each role and section, and the rest follow.
   - One line is added under your summary: **Relevant skills for this role**, listing 10 to 12 skills your CV shows you using in a role or project.
   - The app only rephrases and reorders what your CV already says. It never adds facts, numbers or skills you do not have, and never turns taking part into leading.
4. Read the **Report** under the job. **Fit shown by the CV** compares your original and the tailored CV, both read for this job without knowing which is which (out of 100, and how many of the job's requirements each clearly shows). Your real fit is the same; this shows how much more clearly the new CV makes the case. **Positioning and requirements** shows how the CV presents you and each requirement: *direct*, *transfers*, *related*, *gap*, or *confirm?* (plausible but not on your CV). The report also shows the keyword coverage before and after, job keywords not found word for word, the review of the first draft, changes that were rejected and why, and any ATS warnings. A missing requirement should be handled truthfully, never by inventing a fact. If you do have that experience, add it to your CV first (see section 4).

The report also lists **length advice**: bullets that run past two lines, bullets in one role that describe the same work, and older roles with many bullets. It is advice only: nothing is changed unless you edit the lines yourself.

### Optimised mode: aim for the top score, step by step

Experienced people often leave out techniques, standards and partners they really worked with. Optimised mode looks for them and proposes them in red, for you to confirm or delete before you apply.

1. Choose **Optimised** and click **Tailor CV**.
2. Open the Word file. Red text is a proposal, never a confirmed fact:
   - **[CONFIRM REWORDED LINE ...]** below one of your lines: that line in the job's own terms, to use instead of it in this CV.
   - **[CONFIRM: ...]** at the end of a line: words that line very probably left out, or a question asking for its number.
   - **[CONFIRM EXPERIENCE BEFORE USE]**: a new line, placed after the line it relates to.
   - **[CONFIRM: ...]** at the end of a skills list: a skill in the category it belongs to.
   - **[CONFIRM COMPRESSED VERSION ...]** or **[CONFIRM MERGED ENTRY ...]**: a shorter version of the lines above it (for example, two similar postdocs as one entry that keeps both institutions and their dates). Your own lines stay until you choose otherwise.
3. Under the job, click **Review and edit CV**. For each proposal, edit it to what you actually did and click **Confirm edited fact**, or **Delete suggestion**. You can say why; a deleted proposal is never suggested again for that CV. For a number, write the real figure into the line; never one you could not explain at interview.
4. For each shorter version, compare it with your lines and click **Use the shorter version** or **Keep my lines**.
5. The report leads with the **screening score (ATS)**: original → tailored → **if you confirm** all proposals, with a grade (A is 80 or more, the aim). It is computed the same way every time from the CV's text: the job's keywords in its own words (weighted by how often the posting uses them), the job title, degrees and soft skills, and it lists the keywords still missing. Employers' own systems are not public, so it is the app's estimate built the way ATS match rates are. The **AI screen** line below it (requirements shown, fit for the role) is a model's reading and can vary a little; rewording rarely moves the fit, new confirmed experience can.

The report is kept with the tailored CV. When you come back later, reload the page or open the job in another tab, the card shows it again with the date it was tailored.

### Compare several tailored CVs for one job, step by step

Every time you click **Tailor CV**, the app keeps a new version; nothing is overwritten. That lets you try, for example, one CV with **Level: automatic** and one with **More senior emphasis**, and see which makes the stronger case for the job.

1. Tailor the CV once, then change an option (Level, Length, Emphasis or Audience) and click **Tailor CV** again. Repeat for each version you want to try.
2. Under the job's buttons, the **CV versions** row lists them in the order they were made: *V1 · Automatic*, *V2 · Level: more senior*, and so on, each with its fit score out of 100. Click a version to show its report and download link in the card. (CVs tailored before this feature show as *Tailored*, because their options were not recorded.)
3. Click **Compare versions**. Tick the versions to compare (all are ticked when it opens), then click **Compare**. Your original CV is added automatically. Up to six CVs, the original included, are read in one go by the CV model: one model call.
4. Read the report:
   - **The verdict** at the top names the version that shows your fit most clearly and by how much it beats your original. If no version beats the original, it says so; try another option or check the wording.
   - **The score table** has one column per CV: fit shown out of 100 with its rank, the five readings (function, domain, seniority, leadership, sector, each 0-4), requirements shown, keyword coverage, keywords on the first page, length in words, rejected rewrites and left-out lines, and the reader's first impression. Green and orange numbers show the change against the original; the best value in each row is in bold and the winning column is highlighted.
   - **What each version changed** lists, for each version, what improved, what got worse and what did not change against your original, down to each requirement and keyword gained or lost.
   - **Job requirements shown** and **Job keywords found** are grids of ticks per CV. Rows where the CVs differ come first.
5. Each column has a **.docx** link to download that version, and **Show in card** to make it the version shown on the job card. Choose that version as the source when you write the cover letter.

All CVs are read together, in shuffled order, without the reader being told which is the original, so the scores are comparable. As with the single before/after check, they measure how clearly each CV shows your fit, not your actual fit: your facts are the same in every version. The last comparison is saved with the job and opens again without another model call. If you edit or remove a version afterwards, the report says it is out of date; click **Compare again**. You can also ask the assistant, for example: *"Make a more senior version of my CV for this job and compare it with the first one."*

### Write a cover letter, step by step

1. On the job's card, click **Cover letter / review CV**.
2. Choose the source: your selected CV or a saved tailored CV for this job. If you import an older Word CV for the job, review and save its text first.
3. Click **Write cover letter** and download it. The letter is short (one page) and does not repeat your CV: two examples tied to the job's main requirements and, for a change of field or level, a short honest paragraph on what carries over. With a tailored CV it follows that CV's positioning. Achievements come from the CV and motivation only from your saved career intent. A hiring-manager review improves the draft once.
4. To edit it in the app, open the **Cover letters** tab, change the greeting, paragraphs or closing, click **Save edits**, then export to Word or text.

To revise a saved tailored CV in the app, use **Review and edit CV** in that job's document panel, then **Save reviewed CV**. Edits saved directly to its file in `output/cvs/` update the document shown in the app. Copies downloaded to another folder do not sync back.

To prepare several jobs at once, tick them and choose **Tailor CV + cover letter** in the top bar, then review each document separately. Saved jobs from older searches can still be used.

### The assistant

The right-hand **Assistant** does things for you from plain-language requests. For example, it can run or refine a search, save, track or remove jobs, tailor a CV or write a letter, edit your profile, career intent or a saved document, and add a company. It changes only what you ask for. New facts about your experience are always proposed for your approval before they reach your CV. Every list updates when the assistant finishes.

## 8. If something does not work

| Symptom | Check |
| --- | --- |
| **Install says conda, npm or command line tools are missing** | Install what it names (section 1, *What you need first*), open a **new** terminal, `cd ~/job_search` and run `bash scripts/install.sh` again. |
| **`git clone` fails or `job_search` already exists** | Check the internet connection. If the folder already exists you have the app: `cd ~/job_search` and use `git pull` to update it. |
| **Backend unreachable**, or Chrome cannot open `localhost:8000` | On a Mac, click the Job Search icon. Otherwise run `bash scripts/start.sh` in the `job_search` folder and keep that terminal open. Startup logs are in `.run/logs/`. |
| **The Dock icon does nothing after moving the folder** | Run `bash scripts/macos/make_app.sh` inside the folder's new location. |
| **No AI scores / pre-filter only** | Select a CV, enable **Match against my CV** and **Smart match**, then check Settings shows both models and a working key or CLI sign-in. |
| **No profiles yet** | Click **Build profile from selected CV**. If it fails, check the profile model and its provider credentials in Settings. |
| **A search asks you to select a CV, although one is selected** | Select the CV once more (and pin its profile again if you use one). From now on the app keeps the selection when it restarts. The Gmail alerts source (your LinkedIn and Indeed alerts) needs a selected CV. |
| **A source failed** | Read the reason under **Not used**. A timeout, a server error (5xx) or a dropped connection is usually temporary: click **Retry**. *429* or *999* means the site asked the app to slow down: wait a few minutes, then retry. *403 Forbidden* means the site blocks automated reading; retrying does not help, so use **View on…** and **Paste description**. If Gmail's sign-in lapsed, click **Reconnect Gmail**, then retry. A source missing its API key is listed as skipped, not failed. |
| **No jobs** | Open the source report and notices; widen the date or location, enable relevant sources, and inspect excluded jobs. A newly added company may have no public feed. |
| **Gmail source unavailable** | Check the three `JOBSEARCH_GMAIL_*` values in `.env`, restart, then connect or reconnect under **Job alerts**. Keep a CV selected and Smart match on. |
| **Gmail needs reconnecting every week** | Your Google Cloud project is in **Testing** status, so Google ends its sign-ins after 7 days. Publish it (**Google Auth Platform → Audience → Publish app**), then click **Reconnect Gmail**. After a reconnect, click **Retry** under the results to read the alerts the failed search missed. |
| **Google redirect error** | The authorised redirect URI must be exactly `http://localhost:8000/api/gmail/callback`, unless you deliberately changed the backend redirect setting in `.env`. |
| **Claude keeps showing as signed out** | Usually several programs renewed the same Claude sign-in at once (section 2, B, step 5). Check **Settings → Claude accounts**. If an account says signed out, click **Sign in** for it. If it says signed in, wait up to 10 minutes and the app uses it again. Interrupted saves can leave files named `~/.claude.json.tmp.*` in your home folder; they are safe to delete while no Claude program is running. |
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
