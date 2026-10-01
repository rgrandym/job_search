// Mirrors the backend Pydantic models (src/jobs/models.py, src/services/*). Keep in sync.

export type WorkArrangement = "remote" | "hybrid" | "onsite";
export type Provider = "anthropic" | "openai" | "openrouter" | "codex";
export type Effort = "low" | "medium" | "high" | "xhigh" | "max";

export interface ModelInfo {
  id: string;
  name: string;
  group: string;
  context_length: number | null;
  input_price: number | null;
  output_price: number | null;
  tools: boolean | null;
  structured: boolean | null;
  note: string | null;
}

export interface CodexStatus {
  installed: boolean;
  logged_in: boolean;
  path: string | null;
  message: string;
}

export interface SearchQuery {
  titles: string[];
  keywords: string[];
  locations: string[];
  distance_miles: number | null;
  salary_min: number | null;
  salary_max: number | null;
  work_arrangements: WorkArrangement[];
  sources: string[];
  limit?: number;
}

export interface JobPosting {
  id: string;
  title: string;
  company: string;
  location: string | null;
  work_arrangement: WorkArrangement;
  description: string;
  required_skills: string[];
  preferred_skills: string[];
  salary_range: string | null;
  salary_min: number | null;
  salary_max: number | null;
  url: string | null;
  posted_at: string | null;
  source: string;
}

export interface ScoreBreakdown {
  title: number;
  skills: number;
  experience: number;
  location: number;
  semantic: number;
  total: number;
  matched_skills: string[];
  missing_required_skills: string[];
  notes: string[];
  metrics_used: string[];
}

export interface JobVerdict {
  job_id: string;
  match: boolean;
  fit_score: number;
  verdict: "strong" | "good" | "stretch" | "poor";
  reasons: string[];
  gaps: string[];
  dealbreakers: string[];
}

export interface MatchResult {
  job: JobPosting;
  excluded: boolean;
  exclusion_reasons: string[];
  similarity: number | null;
  score: ScoreBreakdown | null;
  verdict: JobVerdict | null;
  passed: boolean;
}

export interface SkillEvidence {
  skill: string;
  level: "expert" | "proficient" | "familiar";
  evidence: string;
}

export interface ProfileSummary {
  headline: string;
  seniority: string;
  years_experience: number;
  core_expertise: string[];
  key_skills: SkillEvidence[];
  domains: string[];
  target_roles: string[];
  stretch_roles: string[];
  not_a_fit: string[];
  search_keywords: string[];
  summary: string;
}

export interface MatchReport {
  threshold: number;
  matches: MatchResult[];
  below_threshold: MatchResult[];
  excluded: MatchResult[];
  not_retrieved: string[];
  screened: boolean;
  summary: ProfileSummary | null;
}

export interface SearchOutcome {
  report: MatchReport;
  fetched: number;
  errors: Record<string, string>;
  skipped_sources: Record<string, string>;
  summary_from_memory: boolean | null;
  smart_unavailable: string | null;
  seconds: number;
}

export interface LLMView {
  provider: Provider;
  orchestrator_model: string;
  worker_model: string;
  effort: Effort;
  key_set: boolean;
  ready: boolean;
}

export interface CVAsset {
  id: string;
  filename: string;
  size: number;
  kind: "master" | "uploaded" | "generated";
  parsed: boolean;
  selected: boolean;
}

export interface AppState {
  llm: LLMView;
  cv: { name: string; headline: string | null; roles: number; skills: number } | null;
  cv_files: { available: CVAsset[]; selected: string | null };
  sources: { available: string[]; skipped: Record<string, string> };
  agents: { name: string; description: string; role: string }[];
  defaults: { threshold: number };
}

/** Events streamed over /api/ws/chat. */
export type ChatEvent =
  | { type: "session"; session_id: string }
  | { type: "agent_status"; agent: string; depth: number; status: string }
  | { type: "agent_message"; agent: string; depth: number; text: string; final: boolean }
  | { type: "tool_call"; agent: string; depth: number; id: string; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; agent: string; depth: number; id: string; tool: string; ok: boolean; preview: string }
  | { type: "delegate_start"; agent: string; depth: number; task: string }
  | { type: "delegate_end"; agent: string; depth: number }
  | { type: "search_progress"; stage: string; message: string }
  | { type: "search_results"; outcome: SearchOutcome }
  | { type: "profile_summary"; summary: ProfileSummary; from_memory: boolean }
  | { type: "file_ready"; name: string; url: string; job_id: string }
  | { type: "cv_updated"; reason: string }
  | { type: "done"; tokens: { input: number; output: number }; cancelled?: boolean }
  | { type: "error"; message: string };
