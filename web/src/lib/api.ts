import type {
  AppState,
  CodexUsage,
  CodexStatus,
  CVAsset,
  Effort,
  LLMView,
  ModelInfo,
  ProfileSummary,
  Provider,
  SearchOutcome,
  SearchQuery,
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
}

export const api = {
  state: () => request<AppState>("/api/state"),
  models: (provider: Provider) => request<ModelInfo[]>(`/api/llm/models?provider=${provider}`),
  updateLLM: (body: {
    provider: Provider;
    orchestrator_model: string;
    worker_model: string;
    effort: Effort;
    api_key?: string;
  }) => request<LLMView>("/api/llm", json("PUT", body)),
  codexStatus: () => request<CodexStatus>("/api/codex/status"),
  codexUsage: () => request<CodexUsage>("/api/codex/usage"),
  loginCodex: () => request<{ started: boolean }>("/api/codex/login", { method: "POST" }),
  uploadCV: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<CVAsset>("/api/cv/upload", { method: "POST", body: form });
  },
  selectCV: (assetId: string) =>
    request<CVAsset>(`/api/cv/selection/${encodeURIComponent(assetId)}`, { method: "PUT" }),
  profileSummary: (query: SearchQuery, use_cv: boolean, refresh = false) =>
    request<{ summary: ProfileSummary; from_memory: boolean }>(
      "/api/profile-summary",
      json("POST", { query, use_cv, refresh }),
    ),
  search: (body: SearchRequest) => request<SearchOutcome>("/api/search", json("POST", body)),
  tailor: (jobId: string, template: string) =>
    request<{ download_url: string; keyword_coverage: number; missing_keywords: string[]; rejected: number }>(
      `/api/jobs/${encodeURIComponent(jobId)}/tailor`,
      json("POST", { template }),
    ),
};
