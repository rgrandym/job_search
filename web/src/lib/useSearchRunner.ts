import { useQueryClient } from "@tanstack/react-query";
import { useChat } from "../stores/chatStore";
import { profileSearchQuery, useSearch } from "../stores/searchStore";
import { api } from "./api";
import type { SearchOutcome, SearchStreamEvent } from "./types";

/** After a run: the jobs that would match but whose full posting could not be read, if any. */
function toFollowUp(outcome: SearchOutcome): { historyId: string; jobIds: string[] } | null {
  const jobIds = (outcome.report.to_check ?? []).map((r) => r.job.id);
  return outcome.history_id && jobIds.length && !outcome.cancelled
    ? { historyId: outcome.history_id, jobIds }
    : null;
}

/** Start, stop, continue and repeat searches; shared by the sidebar and the results panel. */
export function useSearchRunner() {
  const qc = useQueryClient();

  const onEvent = (event: SearchStreamEvent) => {
    if (event.type === "search_progress") {
      const step = { stage: event.stage, message: event.message, source: event.source, status: event.status, done: event.done, total: event.total, at: Date.now() };
      useSearch.setState((st) => ({ progress: step.message, log: [...st.log, step], beat: null }));
    } else if (event.type === "heartbeat") {
      useSearch.setState({ beat: { waiting_on: event.waiting_on, waiting_s: event.waiting_s, at: Date.now() } });
    } else if (event.type === "model_usage") {
      useChat.getState().recordUsage(event);
    }
  };

  /** Run one streamed job; `keep` leaves the current results on screen while it runs. */
  const start = async (
    label: string,
    call: (runId: string) => Promise<SearchOutcome>,
    keep = false,
    autoFollowUp = true,
  ): Promise<void> => {
    const runId = crypto.randomUUID();
    let followUp: { historyId: string; jobIds: string[] } | null = null;
    useSearch.setState({
      loading: true, error: null, progress: label, log: [], runId, beat: null, startedAt: Date.now(),
      ...(keep ? {} : { outcome: null, openedFrom: null }),
    });
    try {
      const outcome = await call(runId);
      useSearch.setState({ outcome });
      followUp = toFollowUp(outcome);
      if (outcome.report.summary)
        useSearch.setState({ summary: { summary: outcome.report.summary, fromMemory: !!outcome.summary_from_memory } });
      void qc.invalidateQueries({ queryKey: ["profiles"] }); // a search may store a new profile
      void qc.invalidateQueries({ queryKey: ["history"] });
      void qc.invalidateQueries({ queryKey: ["yield"] });
      void qc.invalidateQueries({ queryKey: ["claude-code-usage"] });
    } catch (e) {
      useSearch.setState({ error: (e as Error).message });
    } finally {
      useSearch.setState({ loading: false, progress: null, runId: null, beat: null, startedAt: null });
    }
    // Results first; then the postings that would match but could not be read are read and
    // judged in full, with the results staying on screen (Stop ends it like any run).
    if (followUp && autoFollowUp) {
      const { historyId, jobIds } = followUp;
      await start(`Reading the full postings of ${jobIds.length} job(s) to check…`, (id) =>
        api.recheckStream({ history_id: historyId, run_id: id, job_ids: jobIds }, onEvent), true, false);
    }
  };

  /** A new search with the sidebar's filters (`alertOnly`: only new Gmail alerts). */
  const run = (alertOnly = false) => {
    const s = useSearch.getState();
    const label = alertOnly ? "Reading new job alerts…" : s.smart ? "Searching and screening…" : "Searching…";
    return start(label, (runId) =>
      api.searchStream(
        {
          query: alertOnly
            ? { ...profileSearchQuery(s.query), sources: ["gmail_alerts"] }
            : profileSearchQuery(s.query),
          use_cv: alertOnly ? true : s.useCv,
          smart: alertOnly ? true : s.smart,
          threshold: s.threshold,
          profile_key: s.useCv || alertOnly ? s.profileKey : null,
          widen: s.widen && s.useCv && !alertOnly,
          run_id: runId,
        },
        onEvent,
      ),
    );
  };

  /** Judge the shortlisted jobs a stopped or partly failed search left unjudged. */
  const resume = (historyId: string) =>
    start("Continuing the screening…", (runId) => api.continueStream({ history_id: historyId, run_id: runId }, onEvent), true);

  /** Read the full postings of results whose requirements were not checked (all, or `jobIds`),
   *  or judge one job on its pasted `description`; the other verdicts are kept. */
  const recheck = (historyId: string, jobIds?: string[], description?: string) =>
    start(
      description ? "Judging the pasted description…" : "Opening full postings…",
      (runId) =>
        api.recheckStream({ history_id: historyId, run_id: runId, job_ids: jobIds, description }, onEvent),
      true,
      false, // a manual check never chains another one
    );

  /** Stop the running search; it ends with what it has done so far. */
  const stop = () => {
    const runId = useSearch.getState().runId;
    if (!runId) return;
    useSearch.setState({ progress: "Stopping…" });
    api.stopSearch(runId).catch((e: Error) => useSearch.setState({ error: e.message }));
  };

  return { run, resume, recheck, stop };
}
