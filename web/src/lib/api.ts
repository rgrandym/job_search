import type {
  AppState,
  BoardView,
  CompanyBoard,
  CompanyBoardsStatus,
  CodexUsage,
  CodexStatus,
  ClaudeCodeUsage,
  HistoryItem,
  JobTracking,
  MatchResult,
  LabelReview,
  LearningState,
  UserLabel,
  ATSReport,
  FamilyYield,
  OutcomeReview,
  OutcomeStage,
  QueuedEvidence,
  SearchIntent,
  SavedJob,
  ProfileRecord,
  SourceYield,
  TrackedJob,
  SearchStreamEvent,
  ClaudeCodeStatus,
  CVAsset,
  CoverLetterView,
  Effort,
  LLMView,
  ModelInfo,
  ProfileSummary,
  Provider,
  SearchOutcome,
  SearchQuery,
  TailoredCVView,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  return res.json() as Promise<T>;
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
const continueStream = (body: { history_id: string; run_id: string }, onEvent: (event: SearchStreamEvent) => void) =>
  streamOutcome("/api/search/continue/stream", body, onEvent);

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
  run_id?: string;
}

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
  gmailStatus: () => request<{ configured: boolean; connected: boolean; account: string | null }>("/api/gmail/status"),
  models: (provider: Provider) => request<ModelInfo[]>(`/api/llm/models?provider=${provider}`),
  updateLLM: (body: {
    provider: Provider;
    quality_model: string;
    screening_model: string;
    quality_effort: Effort;
    screening_effort: Effort;
    api_key?: string;
  }) => request<LLMView>("/api/llm", json("PUT", body)),
  codexStatus: () => request<CodexStatus>("/api/codex/status"),
  codexUsage: () => request<CodexUsage>("/api/codex/usage"),
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
  deleteCV: (assetId: string) =>
    request<{ deleted: boolean }>(`/api/cv/${encodeURIComponent(assetId)}`, { method: "DELETE" }),
  exportGeneralCV: () =>
    request<{ download_url: string; roles: number }>("/api/cv/general", { method: "POST" }),
  profileSummary: (query: SearchQuery, use_cv: boolean, refresh = false) =>
    request<{ summary: ProfileSummary; from_memory: boolean; key: string }>(
      "/api/profile-summary",
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
  stopSearch: (runId: string) =>
    request<{ stopped: boolean }>(`/api/search/stop/${encodeURIComponent(runId)}`, { method: "POST" }),
  profiles: () =>
    request<{ cv_parsed: boolean; profiles: ProfileRecord[]; family_yield: FamilyYield[] }>("/api/profiles"),
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
  refreshProfile: (key: string) =>
    request<ProfileRecord>(`/api/profiles/refresh/${encodeURIComponent(key)}`, { method: "POST" }),
  deleteProfile: (key: string) =>
    request<{ deleted: boolean }>(`/api/profiles/${encodeURIComponent(key)}`, { method: "DELETE" }),
  claudeCodeUsage: () => request<ClaudeCodeUsage>("/api/claude-code/usage"),
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
  sourceYield: () => request<SourceYield[]>("/api/sources/yield"),
  tailor: (jobId: string, template: string, result?: MatchResult,
    emphasis: "auto" | "leadership" | "hands_on" = "auto",
    level: "auto" | "senior" | "junior" = "auto") =>
    request<{
      document_id: string;
      download_url: string;
      keyword_coverage: number;
      missing_keywords: string[];
      restored_keywords: string[];
      critique: string[];
      ats: ATSReport | null;
      source_ats_keyword_coverage: number | null;
      rejected: number;
      rejections: Rejection[];
      tracking: JobTracking | null;
    }>(
      `/api/jobs/${encodeURIComponent(jobId)}/tailor`,
      json("POST", { template, result, emphasis, level }),
    ),
  tailoredCVs: (jobId: string) =>
    request<TailoredCVView[]>(`/api/jobs/${encodeURIComponent(jobId)}/tailored-cvs`),
  allTailoredCVs: () => request<TailoredCVView[]>("/api/tailored-cvs"),
  importOlderCV: (asset_id: string, title: string, company: string, description: string) =>
    request<TailoredCVView>("/api/tailored-cvs/import", json("POST", { asset_id, title, company, description })),
  importTailoredCV: (jobId: string, assetId: string, result?: MatchResult) =>
    request<TailoredCVView>(`/api/jobs/${encodeURIComponent(jobId)}/tailored-cvs/import`,
      json("POST", { asset_id: assetId, result })),
  editTailoredCV: (id: string, edits: { headline?: string; summary?: string; bullets?: Record<string, string> }) =>
    request<TailoredCVView>(`/api/tailored-cvs/${encodeURIComponent(id)}`, json("PUT", edits)),
  coverLetter: (jobId: string, template: string, result?: MatchResult, tailored_cv_id?: string) =>
    request<{ download_url: string; paragraphs: number; rejections: Rejection[] }>(
      `/api/jobs/${encodeURIComponent(jobId)}/cover-letter`,
      json("POST", { template, result, tailored_cv_id }),
    ),
  coverLetters: () => request<CoverLetterView[]>("/api/cover-letters"),
  editCoverLetter: (id: string, edits: Pick<CoverLetterView, "greeting" | "paragraphs" | "closing">) =>
    request<CoverLetterView>(`/api/cover-letters/${encodeURIComponent(id)}`, json("PUT", edits)),
  deleteCoverLetter: (id: string) =>
    request<{ deleted: number }>(`/api/cover-letters/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteAllCoverLetters: () =>
    request<{ deleted: number }>("/api/cover-letters", { method: "DELETE" }),
};
