import type {
  LetterScore,
  CVHealth,
  AppState,
  LetterFlag,
  Audience,
  BoardView,
  CompanyBoard,
  CompanyBoardsStatus,
  ConnectionsView,
  CodexStatus,
  OnLimit,
  HistoryItem,
  JobTracking,
  JobPosting,
  OneJobQuery,
  OneJobResults,
  MatchResult,
  LabelReview,
  LearningState,
  UserLabel,
  FamilyYield,
  OutcomeReview,
  OutcomeStage,
  QueuedEvidence,
  SearchIntent,
  TaskProgress,
  SavedJob,
  ProfileRecord,
  SourceYield,
  TrackedJob,
  SearchStreamEvent,
  ClaudeCodeStatus,
  CVAsset,
  MasterCV,
  CoverLetterView,
  Effort,
  LLMView,
  ModelInfo,
  ProfileSummary,
  Provider,
  SearchOutcome,
  SearchQuery,
  TailoredCVView,
  TailorReport,
  VersionComparison,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await checked(path, init);
  return res.json() as Promise<T>;
}

/** Fetch, turning an error response into an Error carrying the server's detail. */
async function checked(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res;
}

/** POST to a streaming search endpoint and deliver each NDJSON event as it arrives;
 * resolves with the outcome. */
async function streamOutcome(path: string, body: unknown, onEvent: (event: SearchStreamEvent) => void): Promise<SearchOutcome> {
  const res = await fetch(path, json("POST", body));
  if (!res.ok || !res.body) throw new Error(res.statusText || `HTTP ${res.status}`);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let outcome: SearchOutcome | null = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line) as SearchStreamEvent;
      if (event.type === "error") throw new Error(event.message);
      if (event.type === "search_results") outcome = event.outcome;
      onEvent(event);
    }
  }
  if (!outcome) throw new Error("The search ended without results");
  return outcome;
}

const searchStream = (body: SearchRequest, onEvent: (event: SearchStreamEvent) => void) =>
  streamOutcome("/api/search/stream", body, onEvent);

/** Judge the shortlisted jobs a stopped or partly failed search left unjudged. */
const continueStream = (body: { history_id: string; run_id: string; switch?: boolean }, onEvent: (event: SearchStreamEvent) => void) =>
  streamOutcome("/api/search/continue/stream", body, onEvent);
/** Read full postings (or judge pasted text) for results whose requirements were not checked. */
const recheckStream = (
  body: { history_id: string; run_id: string; job_ids?: string[]; description?: string },
  onEvent: (event: SearchStreamEvent) => void,
) => streamOutcome("/api/search/recheck/stream", body, onEvent);

/** Search again only the sources that failed in a past search (all failed ones, or `sources`). */
const retryStream = (
  body: { history_id: string; run_id: string; sources?: string[] },
  onEvent: (event: SearchStreamEvent) => void,
) => streamOutcome("/api/search/retry/stream", body, onEvent);

/** `?progress_id=…` so the server reports this request's steps (see `TaskProgress`). */
const tracked = (path: string, progressId?: string) =>
  progressId ? `${path}${path.includes("?") ? "&" : "?"}progress_id=${encodeURIComponent(progressId)}` : path;

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export interface SearchRequest {
  query: SearchQuery;
  use_cv: boolean;
  smart: boolean;
  threshold: number;
  refresh_summary?: boolean;
  profile_key?: string | null;
  /** Also search the adjacent role families the profile proposes. */
  widen?: boolean;
  /** Listing threshold per role area; areas left out use `threshold`. */
  area_thresholds?: AreaThresholds;
  run_id?: string;
}

/** Role areas whose listing threshold can differ from the search's (core roles use it). */
export type AreaTier = "progression" | "adjacent";
export type AreaThresholds = Partial<Record<AreaTier, number>>;

export interface TrackingUpdate {
  status: "open" | "applied" | "na";
  note?: string;
  reason?: string;
  stage?: OutcomeStage;
}

export interface Rejection {
  source_id: string;
  reason: string | null;
}

export const api = {
  state: () => request<AppState>("/api/state"),
  companiesStatus: () => request<CompanyBoardsStatus>("/api/companies/status"),
  companyBoards: () => request<BoardView[]>("/api/companies/boards"),
  myCompanies: () => request<CompanyBoard[]>("/api/companies/mine"),
  /** Finds the company's job feed now; fails with the reason when there is none to read. */
  addCompany: (name: string, url: string) => request<CompanyBoard>("/api/companies/mine", json("POST", { name, url })),
  removeCompany: (name: string) =>
    request<{ removed: boolean }>(`/api/companies/mine/${encodeURIComponent(name)}`, { method: "DELETE" }),
  discoverCompanies: (mode: "new" | "stale" | "all" = "stale") =>
    request<CompanyBoardsStatus>(`/api/companies/discover?mode=${mode}`, { method: "POST" }),
  stopDiscovery: () => request<CompanyBoardsStatus>("/api/companies/discover/stop", { method: "POST" }),
  gmailStatus: () =>
    request<{ configured: boolean; connected: boolean; account: string | null; problem: string | null }>("/api/gmail/status"),
  models: (provider: Provider) => request<ModelInfo[]>(`/api/llm/models?provider=${provider}`),
  chatProviders: () => request<Record<Provider, boolean>>("/api/llm/chat-providers"),
  updateLLM: (body: {
    provider: Provider;
    quality_model: string;
    screening_model: string;
    quality_effort: Effort;
    screening_effort: Effort;
    profile_model: string;
    profile_effort: Effort;
    profile_provider: Provider | null;
    cv_model: string;
    cv_effort: Effort;
    cv_provider: Provider | null;
    letter_model: string;
    letter_effort: Effort;
    letter_provider: Provider | null;
    api_key?: string;
    profile_api_key?: string;
    cv_api_key?: string;
    letter_api_key?: string;
  }) => request<LLMView>("/api/llm", json("PUT", body)),
  codexStatus: () => request<CodexStatus>("/api/codex/status"),
  connections: () => request<ConnectionsView>("/api/connections"),
  updateHandover: (body: { on_limit?: OnLimit; handover_threshold?: number }) =>
    request<LLMView>("/api/llm/handover", json("PUT", body)),
  loginCodex: () => request<{ started: boolean }>("/api/codex/login", { method: "POST" }),
  claudeCodeStatus: () => request<ClaudeCodeStatus>("/api/claude-code/status"),
  loginClaudeCode: () => request<{ started: boolean }>("/api/claude-code/login", { method: "POST" }),
  uploadCV: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<CVAsset>("/api/cv/upload", { method: "POST", body: form });
  },
  selectCV: (assetId: string) =>
    request<CVAsset>(`/api/cv/selection/${encodeURIComponent(assetId)}`, { method: "PUT" }),
  editableCV: () => request<MasterCV>("/api/cv/editable"),
  saveCV: (cv: MasterCV) => request<MasterCV>("/api/cv", json("PUT", cv)),
  deleteCV: (assetId: string) =>
    request<{ deleted: boolean }>(`/api/cv/${encodeURIComponent(assetId)}`, { method: "DELETE" }),
  cvHealth: () => request<CVHealth>("/api/cv/health"),
  cvAtsCopy: () => request<{ id: string; filename: string }>("/api/cv/health/ats-copy", { method: "POST" }),
  exportGeneralCV: (progressId?: string) =>
    request<{ download_url: string; roles: number }>(tracked("/api/cv/general", progressId), { method: "POST" }),
  taskProgress: (taskId: string) => request<TaskProgress>(`/api/progress/${encodeURIComponent(taskId)}`),
  /** Stop a running task now (tailoring, a letter, a profile build or update, a CV export). */
  stopTask: (taskId: string) =>
    request<{ stopped: boolean }>(`/api/progress/${encodeURIComponent(taskId)}/stop`, { method: "POST" }),
  cvSourceUrl: (assetId: string) => `/api/cv/source/${encodeURIComponent(assetId)}`,
  cvRevision: (assetId: string) => request<{ revision: string }>(`/api/cv/revision/${encodeURIComponent(assetId)}`),
  /** The CV as the browser can show it (Word files arrive as an HTML page view). */
  cvPreview: async (assetId: string) =>
    (await checked(`/api/cv/preview/${encodeURIComponent(assetId)}`, { cache: "no-store" })).blob(),
  openCVSource: (assetId: string, app: "default" | "word" = "default") =>
    request<{ opened: boolean }>(`/api/cv/open/${encodeURIComponent(assetId)}?app=${app}`, { method: "POST" }),
  profileSummary: (query: SearchQuery, use_cv: boolean, refresh = false, progressId?: string) =>
    request<{ summary: ProfileSummary; from_memory: boolean; key: string }>(
      tracked("/api/profile-summary", progressId),
      json("POST", { query, use_cv, refresh }),
    ),
  search: (body: SearchRequest) => request<SearchOutcome>("/api/search", json("POST", body)),
  history: () => request<HistoryItem[]>("/api/history"),
  openHistory: (id: string) =>
    request<{ request: SearchRequest; outcome: SearchOutcome }>(`/api/history/${encodeURIComponent(id)}`),
  deleteHistory: (id: string) =>
    request<{ deleted: boolean }>(`/api/history/${encodeURIComponent(id)}`, { method: "DELETE" }),
  clearHistory: () => request<{ cleared: boolean }>("/api/history", { method: "DELETE" }),
  searchStream,
  continueStream,
  recheckStream,
  retryStream,
  removeResult: (jobId: string, historyId: string | null) =>
    request<{ removed: boolean }>(
      `/api/results/${encodeURIComponent(jobId)}${historyId ? `?history_id=${encodeURIComponent(historyId)}` : ""}`,
      { method: "DELETE" },
    ),
  stopSearch: (runId: string) =>
    request<{ stopped: boolean }>(`/api/search/stop/${encodeURIComponent(runId)}`, { method: "POST" }),
  profiles: () =>
    request<{ cv_parsed: boolean; profiles: ProfileRecord[]; other_profiles: ProfileRecord[]; family_yield: FamilyYield[] }>("/api/profiles"),
  intent: () => request<SearchIntent>("/api/intent"),
  saveIntent: (intent: SearchIntent) => request<SearchIntent>("/api/intent", json("PUT", intent)),
  outcomesReview: () => request<OutcomeReview>("/api/outcomes/review"),
  labels: () => request<LabelReview>("/api/labels"),
  learning: () => request<LearningState>("/api/learning"),
  /** One quality-model call: general preferences proposed from your labels and notes. */
  suggestPreferences: () => request<LearningState>("/api/learning/suggest", { method: "POST" }),
  decidePreference: (id: string, accept: boolean, text?: string) =>
    request<LearningState>(`/api/learning/${encodeURIComponent(id)}`, json("POST", { accept, text })),
  removePreference: (id: string) =>
    request<LearningState>(`/api/learning/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** Your call on a job of the current search (null removes it); never affects searches. */
  labelJob: (jobId: string, label: UserLabel | null) =>
    request<{ label: UserLabel | null }>(`/api/jobs/${encodeURIComponent(jobId)}/label`, json("PUT", { label })),
  saved: () => request<SavedJob[]>("/api/saved"),
  findOneJob: (query: OneJobQuery, progressId?: string) =>
    request<OneJobResults>(tracked("/api/jobs/find-one", progressId), json("POST", query)),
  saveFoundJob: (job: JobPosting) => request<SavedJob>("/api/saved/found", json("POST", job)),
  /** Read a saved job's full posting again, or keep the pasted `description`. */
  refreshSavedPosting: (jobId: string, description: string | undefined, progressId: string) =>
    request<SavedJob>(tracked(`/api/saved/${encodeURIComponent(jobId)}/posting`, progressId), json("POST", { description: description ?? null })),
  importJob: (job: { title: string; company: string; description: string; url?: string; location?: string }) =>
    request<SavedJob>("/api/saved/import", json("POST", job)),
  saveJobs: (job_ids: string[]) => request<SavedJob[]>("/api/saved", json("POST", { job_ids })),
  removeSaved: (job_ids: string[]) => request<{ removed: number }>("/api/saved/remove", json("POST", { job_ids })),
  evidence: () => request<QueuedEvidence[]>("/api/evidence"),
  evidenceUpload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<QueuedEvidence[]>("/api/evidence/upload", { method: "POST", body: form });
  },
  evidenceText: (source: string, text: string) =>
    request<QueuedEvidence[]>("/api/evidence/text", json("POST", { source, text })),
  decideEvidence: (id: string, accept: boolean, text?: string) =>
    request<QueuedEvidence>(`/api/evidence/${encodeURIComponent(id)}`, json("POST", { accept, text })),
  editProfile: (key: string, summary: ProfileSummary) =>
    request<ProfileRecord>(`/api/profiles/${encodeURIComponent(key)}`, json("PUT", summary)),
  refreshProfile: (key: string, progressId?: string) =>
    request<ProfileRecord>(tracked(`/api/profiles/refresh/${encodeURIComponent(key)}`, progressId), { method: "POST" }),
  deleteProfile: (key: string) =>
    request<{ deleted: boolean }>(`/api/profiles/${encodeURIComponent(key)}`, { method: "DELETE" }),
  /** Mark a job of the current search applied / N/A / open, and/or set its note, reason or stage. */
  trackJob: (jobId: string, status?: "open" | "applied" | "na", note?: string, extra?: Omit<TrackingUpdate, "status" | "note">) =>
    request<JobTracking>(`/api/jobs/${encodeURIComponent(jobId)}/tracking`, json("PUT", { status, note, ...extra })),
  tracker: () => request<TrackedJob[]>("/api/tracker"),
  addApplication: (body: { title: string; company: string; url?: string; note?: string }) =>
    request<TrackedJob>("/api/tracker", json("POST", body)),
  editTracked: (id: string, status?: "open" | "applied" | "na", note?: string, extra?: Omit<TrackingUpdate, "status" | "note">) =>
    request<TrackedJob>(`/api/tracker/${encodeURIComponent(id)}`, json("PUT", { status, note, ...extra })),
  deleteTracked: (id: string) =>
    request<{ deleted: boolean }>(`/api/tracker/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** Remove applications made before `before` (YYYY-MM-DD) from the register. */
  clearApplied: (before: string) =>
    request<{ removed: number }>("/api/tracker/clear-applied", json("POST", { before })),
  sourceYield: () => request<SourceYield[]>("/api/sources/yield"),
  tailor: (jobId: string, template: string, result?: MatchResult,
    emphasis: "auto" | "leadership" | "hands_on" = "auto",
    level: "auto" | "senior" | "junior" = "auto",
    length: "auto" | "full" | "junior" = "auto", progressId?: string,
    audience: Audience = "auto", mode: "standard" | "optimised" = "standard") =>
    request<TailorReport & { tracking: JobTracking | null }>(
      tracked(`/api/jobs/${encodeURIComponent(jobId)}/tailor`, progressId),
      json("POST", { template, result, emphasis, level, length, audience, mode }),
    ),
  tailoredCVs: (jobId: string) =>
    request<TailoredCVView[]>(`/api/jobs/${encodeURIComponent(jobId)}/tailored-cvs`),
  allTailoredCVs: () => request<TailoredCVView[]>("/api/tailored-cvs"),
  cvComparison: (jobId: string) =>
    request<{ comparison: VersionComparison | null; stale: boolean; baseline: string | null }>(
      `/api/jobs/${encodeURIComponent(jobId)}/cv-comparison`),
  compareCVs: (jobId: string, documentIds: string[], baselineId: string | null, progressId?: string) =>
    request<VersionComparison>(
      tracked(`/api/jobs/${encodeURIComponent(jobId)}/cv-comparison`, progressId),
      json("POST", { document_ids: documentIds, baseline_id: baselineId }),
    ),
  importOlderCV: (asset_id: string, title: string, company: string, description: string) =>
    request<TailoredCVView>("/api/tailored-cvs/import", json("POST", { asset_id, title, company, description })),
  importTailoredCV: (jobId: string, assetId: string, result?: MatchResult) =>
    request<TailoredCVView>(`/api/jobs/${encodeURIComponent(jobId)}/tailored-cvs/import`,
      json("POST", { asset_id: assetId, result })),
  editTailoredCV: (id: string, edits: { headline?: string; summary?: string; bullets?: Record<string, string> }) =>
    request<TailoredCVView>(`/api/tailored-cvs/${encodeURIComponent(id)}`, json("PUT", edits)),
  reviewCVAddition: (id: string, additionId: string, decision: "confirm" | "delete", text?: string, reason?: string) =>
    request<TailoredCVView>(
      `/api/tailored-cvs/${encodeURIComponent(id)}/additions/${encodeURIComponent(additionId)}`,
      json("PUT", { decision, text, reason: reason ?? "" }),
    ),
  finalTailoredCV: (id: string) =>
    request<TailoredCVView>(`/api/tailored-cvs/${encodeURIComponent(id)}/final`, { method: "POST" }),
  reviewCVCompression: (id: string, compressionId: string, decision: "apply" | "reject") =>
    request<TailoredCVView>(
      `/api/tailored-cvs/${encodeURIComponent(id)}/compressions/${encodeURIComponent(compressionId)}`,
      json("PUT", { decision }),
    ),
  coverLetter: (jobId: string, template: string, result?: MatchResult, tailored_cv_id?: string, progressId?: string) =>
    request<{ id: string | null; download_url: string; paragraphs: number; flags: LetterFlag[]; notes: string[]; score: LetterScore | null }>(
      tracked(`/api/jobs/${encodeURIComponent(jobId)}/cover-letter`, progressId),
      json("POST", { template, result, tailored_cv_id }),
    ),
  coverLetters: () => request<CoverLetterView[]>("/api/cover-letters"),
  /** The letter as an HTML page view. */
  coverLetterPreview: async (id: string) =>
    (await checked(`/api/cover-letters/${encodeURIComponent(id)}/preview`, { cache: "no-store" })).blob(),
  openCoverLetter: (id: string, app: "default" | "word" = "word") =>
    request<{ opened: boolean }>(`/api/cover-letters/${encodeURIComponent(id)}/open?app=${app}`, { method: "POST" }),
  editCoverLetter: (id: string, edits: Pick<CoverLetterView, "greeting" | "paragraphs" | "closing"> & { keep?: number[] }) =>
    request<CoverLetterView>(`/api/cover-letters/${encodeURIComponent(id)}`, json("PUT", edits)),
  deleteCoverLetter: (id: string) =>
    request<{ deleted: number }>(`/api/cover-letters/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteAllCoverLetters: () =>
    request<{ deleted: number }>("/api/cover-letters", { method: "DELETE" }),
};
