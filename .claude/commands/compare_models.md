---
description: Score the model/effort setups in model_compare.toml against your job labels
argument-hint: "[plan.toml] (default: model_compare.toml)"
---

Run the model comparison plan. The user invoking this command authorizes the paid model calls it makes.

1. Plan file: `$ARGUMENTS` if given, otherwise `model_compare.toml`. Read it and state, in one
   line, the reference (labels or history), how many labelled jobs (or the sample size) and
   the setups that will run, with a rough call count (labels: about 40 calls per setup).
2. Run it in the background (several minutes per setup):
   `conda activate job_search && python -m src.services.model_compare --config <plan file>`
   Do not change the plan file or pass other flags. Each setup's result prints as it finishes.
3. While it runs, report the latest progress line from the log when asked (setup n/N, jobs
   judged, second opinions, elapsed). When it finishes, report the summary table, then for each
   setup: agreement, good jobs kept (of your Would apply), bad jobs let through (of your No),
   ranking, time, calls, cost per model (API-equivalent; "no price known" without a price in
   `[prices]`) and the share of the plan's session and week windows it used.
   List the good jobs each setup missed: a missed job you would apply for costs more than a
   bad one let through (you can skip a bad match; you never see a missed one).
4. Recommend the cheapest, fastest setup whose good jobs kept and ranking are within about
   two jobs / five points of the best setup (smaller gaps are noise at this sample size), and
   say whether more labels would be needed to separate the leaders. If the run failed, show
   the error.
