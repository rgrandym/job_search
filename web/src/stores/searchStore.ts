import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AreaThresholds } from "../lib/api";
import type {
  Heartbeat, HistoryItem, JobTracking, MatchReport, ProfileSummary, ProgressEvent, SearchOutcome, SearchQuery,
} from "../lib/types";

export const EMPTY_QUERY: SearchQuery = {
  titles: [],
  keywords: [],
  locations: [],
  country: null,
  distance_miles: 25,
  salary_min: null,
  salary_max: null,
  seniority_min: null,
  seniority_max: null,
  work_arrangements: [],
  posted_within_days: null,
  sources: [],
  exclude_boards: [],
};

export const DEFAULT_MATCH_THRESHOLD = 60;

/** Hidden legacy filters must never constrain a new profile-led search. */
export const profileSearchQuery = (query: SearchQuery): SearchQuery => ({
  ...query,
  titles: [],
  keywords: [],
  salary_min: null,
  salary_max: null,
  work_arrangements: [],
});

interface SearchState {
  query: SearchQuery;
  useCv: boolean;
  smart: boolean;
  /** Also search the adjacent role families the profile proposes (tough market). */
  widen: boolean;
  threshold: number;
  /** Own listing threshold for progression or adjacent roles (absent: the match threshold). */
  areaThresholds: AreaThresholds;
  outcome: SearchOutcome | null;
  summary: { summary: ProfileSummary; fromMemory: boolean } | null;
  loading: boolean;
  progress: string | null;
  log: ProgressEvent[];
  /** Latest heartbeat of the running search (cleared by each new progress event). */
  beat: Heartbeat | null;
  /** When the running search started (ms), for its elapsed time. */
  startedAt: number | null;
  profileKey: string | null;
  /** Sources switched off in a category's settings: switching the category back on skips them. */
  disabledSources: string[];
  /** Set while showing a search reopened from the history. */
  openedFrom: { id: string; label: string; createdAt: string; models: string | null } | null;
  /** The saved search on screen last, reopened on reload and in new tabs (persisted).
   *  undefined: never recorded (the newest saved search is opened); null: the user cleared it. */
  lastSearchId: string | null | undefined;
  /** Id of the running search (or continuation), so it can be stopped. */
  runId: string | null;
  error: string | null;
  /** Job ids ticked in the results or saved jobs (not persisted). */
  selected: string[];
  toggleSelected: (jobId: string) => void;
  setQuery: (patch: Partial<SearchQuery>) => void;
  /** Show a job's new status/note wherever it appears in the current results. */
  setTracking: (jobId: string, tracking: JobTracking) => void;
  /** Take a deleted job out of the current results. */
  removeJob: (jobId: string) => void;
  set: (patch: Partial<Omit<SearchState, "set" | "setQuery" | "setTracking" | "removeJob" | "toggleSelected">>) => void;
  resetSearch: () => void;
}

const BUCKETS = ["matches", "to_check", "below_threshold", "excluded", "applied", "dismissed"] as const;

function withTracking(report: MatchReport, jobId: string, tracking: JobTracking): MatchReport {
  const next = { ...report };
  for (const bucket of BUCKETS) {
    const items = report[bucket];
    if (items) next[bucket] = items.map((r) => (r.job.id === jobId ? { ...r, tracking } : r));
  }
  return next;
}

function withoutJob(report: MatchReport, jobId: string): MatchReport {
  const next = { ...report };
  for (const bucket of BUCKETS) {
    const items = report[bucket];
    if (items) next[bucket] = items.filter((r) => r.job.id !== jobId);
  }
  return next;
}

export const useSearch = create<SearchState>()(
  persist(
    (set) => ({
      query: EMPTY_QUERY,
      useCv: true,
      smart: true,
      widen: false,
      threshold: DEFAULT_MATCH_THRESHOLD,
      areaThresholds: {},
      outcome: null,
      summary: null,
      loading: false,
      progress: null,
      log: [],
      beat: null,
      startedAt: null,
      profileKey: null,
      disabledSources: ["demo"],
      openedFrom: null,
      lastSearchId: undefined,
      runId: null,
      error: null,
      selected: [],
      toggleSelected: (jobId) =>
        set((s) => ({
          selected: s.selected.includes(jobId) ? s.selected.filter((id) => id !== jobId) : [...s.selected, jobId],
        })),
      setQuery: (patch) => set((s) => ({ query: { ...s.query, ...patch } })),
      setTracking: (jobId, tracking) =>
        set((s) =>
          s.outcome ? { outcome: { ...s.outcome, report: withTracking(s.outcome.report, jobId, tracking) } } : {},
        ),
      removeJob: (jobId) =>
        set((s) => ({
          selected: s.selected.filter((id) => id !== jobId),
          ...(s.outcome ? { outcome: { ...s.outcome, report: withoutJob(s.outcome.report, jobId) } } : {}),
        })),
      set: (patch) => set(patch),
      resetSearch: () => set({
        query: { ...EMPTY_QUERY },
        useCv: true,
        smart: true,
        widen: false,
        threshold: DEFAULT_MATCH_THRESHOLD,
        areaThresholds: {},
        profileKey: null,
        disabledSources: ["demo"],
        outcome: null,
        summary: null,
        log: [],
        error: null,
        openedFrom: null,
        lastSearchId: null,
        selected: [],
      }),
    }),
    {
      name: "jobsearch.filters",
      // v1: the match floor moved from 70 to 60 (a "reasonable stretch" is listed)
      // v2: sources grouped by category; demo jobs start switched off
      // v3: title, keyword, salary and arrangement controls were removed
      version: 3,
      migrate: (persisted, version) => {
        const state = persisted as Pick<
          SearchState,
          "query" | "useCv" | "smart" | "threshold" | "profileKey" | "disabledSources"
        >;
        const moved = version < 1 && state.threshold === 70 ? { ...state, threshold: 60 } : state;
        const withSources = version < 2 ? { ...moved, disabledSources: ["demo"] } : moved;
        return version < 3 && withSources.query
          ? { ...withSources, query: profileSearchQuery({ ...EMPTY_QUERY, ...withSources.query }) }
          : withSources;
      },
      partialize: (s) => ({
        query: s.query,
        useCv: s.useCv,
        smart: s.smart,
        widen: s.widen,
        threshold: s.threshold,
        areaThresholds: s.areaThresholds,
        profileKey: s.profileKey,
        disabledSources: s.disabledSources,
        lastSearchId: s.lastSearchId,
      }),
    },
  ),
);

// Whatever shows a saved search's results (a search, a continuation, the assistant, the
// history list) makes it the one reopened next time.
useSearch.subscribe((s) => {
  const id = s.outcome?.history_id;
  if (id && id !== s.lastSearchId) useSearch.setState({ lastSearchId: id });
});

/** Show historical results without changing the user's current search settings. */
export function openedSearch(outcome: SearchOutcome, item: HistoryItem): Partial<SearchState> {
  return {
    outcome,
    log: [],
    error: null,
    openedFrom: { id: item.id, label: item.label, createdAt: item.created_at, models: item.models },
    lastSearchId: item.id,
  };
}
