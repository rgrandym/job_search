// Mirrors the backend Pydantic models (src/jobs/models.py, src/services/*). Keep in sync.

export type WorkArrangement = "remote" | "hybrid" | "onsite";
export type Provider = "anthropic" | "openai" | "openrouter" | "codex" | "claude_code";
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

export interface ClaudeCodeStatus extends CodexStatus {
  subscription: string | null;
}

export interface UsageWindow {
  used_percent: number;
  remaining_percent: number;
  window_minutes: number | null;
  resets_at: number | null;
}

export interface CodexUsage {
  plan_type: string | null;
  ordinary_usage_allowed: boolean | null;
  primary: UsageWindow | null;
  secondary: UsageWindow | null;
  credits: { has_credits: boolean; unlimited: boolean; balance: string | null };
  lifetime_tokens: number | null;
  updated_at: number;
}

/** Company job-board discovery (backend `company_discovery.DiscoveryStatus`). */
export interface CompanyBoardsStatus {
  directory: string;
  running: boolean;
  message: string;
  companies: number;
  checked: number;
  /** Companies not checked yet; -1 = the directory has never been read. */
  unchecked: number;
  stale: number;
  boards: number;
  by_ats: Record<string, number>;
  updated_on: string | null;
  error: string | null;
}

export interface SearchQuery {
  titles: string[];
  keywords: string[];
  locations: string[];
  /** Country to search in (e.g. "United Kingdom"); cities in `locations` are within it. */
  country?: string | null;
  distance_miles: number | null;
  salary_min: number | null;
  salary_max: number | null;
  /** Optional hard limits on the advertised job level; null means no level filter. */
  seniority_min?: number | null;
  seniority_max?: number | null;
  work_arrangements: WorkArrangement[];
  /** Only postings from the last N days (1 = 24 h); null = any time. Undated postings are kept. */
  posted_within_days?: number | null;
  sources: string[];
  /** Company job boards (`BoardView.key`) switched off for searches. */
  exclude_boards?: string[];
  limit?: number;
}

export type SourceCategory = "job_boards" | "company" | "alerts";

/** A selectable source and its UI group (backend `fetcher.SOURCE_CATALOG`). */
export interface SourceInfo {
  name: string;
  label: string;
  category: SourceCategory;
  note: string;
}

/** One company job board (several companies can share one). */
/** A company job feed on the watch-list (backend `companies.CompanyBoard`); `origin` is
 * absent for companies the user added by hand. */
export interface CompanyBoard {
  name: string;
  ats: string;
  token?: string | null;
  url?: string | null;
  origin?: string | null;
}

export interface BoardView {
  key: string;
  ats: string;
  companies: string[];
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
  /** The board may have estimated the salary; it is shown but never filtered on. */
  salary_maybe_estimated?: boolean;
  url: string | null;
  posted_at: string | null;
  /** Last day to apply, when the source states one. Closed postings are excluded. */
  closes_at?: string | null;
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

/** Points per dimension; maxima in FIT_WEIGHTS (they sum to the 0-100 fit score). */
export interface FitDimensions {
  function: number;
  domain: number;
  seniority: number;
  leadership: number;
  sector: number;
  practicality: number;
}

/** The matcher's level (0-4) per dimension; code turns levels into points. */
export type FitRatings = Record<keyof FitDimensions, number>;

export interface JobVerdict {
  job_id: string;
  /** null for searches made before levels were introduced. */
  ratings?: FitRatings | null;
  dimensions: FitDimensions;
  /** A match or near the threshold, so a second independent assessment was averaged in. */
  reviewed?: boolean;
  /** Judged in an earlier search (same posting, profile and model): the same verdict again. */
  from_memory?: boolean;
  fit_summary: string;
  reasons: string[];
  transferable: string[];
  gaps: string[];
  essential_unmet: string[];
  unknowns: string[];
  dealbreakers: string[];
  fit_score: number;
  band: "exceptional" | "very_strong" | "strong" | "stretch" | "weak";
  priority: "apply_now" | "worth_applying" | "consider" | "low";
  match: boolean;
  /** Within 5 points of the threshold (after the second opinion, if any). */
  borderline: boolean;
  cap_reason: string | null;
  /** False: too little posting text to check its requirements (fetch or paste it). */
  requirements_checked: boolean;
  /** Fit with the career intent (never changes the score; "against" caps priority). */
  alignment?: Alignment;
  alignment_note?: string;
}

export type Alignment = "against" | "neutral" | "aligned";

/** new: first seen in this search · open: seen before · applied · na: ruled out by the user. */
export type JobStatus = "new" | "open" | "applied" | "na";

/** How an application went (backend `OutcomeStage`). */
export type OutcomeStage =
  | "screening"
  | "interview"
  | "final_round"
  | "offer"
  | "accepted"
  | "rejected"
  | "no_response"
  | "withdrawn";

/** Where the user stands with a job across searches (backend `JobTracking`). */
export interface JobTracking {
  status: JobStatus;
  /** The user's own note; the app never rewrites it. */
  note: string;
  first_seen: string;
  applied_at: string | null;
  cv_file: string | null;
  /** An application to another role at the same employer. */
  related: string | null;
  /** The user's reason it was (or was not) suitable. */
  reason?: string;
  stage?: OutcomeStage | null;
}

export interface MatchResult {
  job: JobPosting;
  excluded: boolean;
  exclusion_reasons: string[];
  similarity: number | null;
  score: ScoreBreakdown | null;
  verdict: JobVerdict | null;
  passed: boolean;
  tracking?: JobTracking | null;
  /** Role family whose search found it. */
  family?: string | null;
  /** Eligibility points to check (language level, right to work, clearance), with evidence. */
  flags?: string[];
}

/** A job the user saved from a search (backend `SavedJob`); tracking is read live. */
export interface SavedJob {
  result: MatchResult;
  saved_at: string;
}

/** A role in the applications register (backend `tracker.TrackedJob`). */
export interface TrackedJob {
  id: string;
  title: string;
  company: string;
  location: string | null;
  url: string | null;
  job_id: string | null;
  source: string;
  /** Stored status: "seen" jobs show as New/Open in results. */
  status: "seen" | "applied" | "na";
  note: string;
  first_seen: string;
  last_seen: string;
  applied_at: string | null;
  cv_file: string | null;
  reason?: string;
  stage?: OutcomeStage | null;
  stages?: { stage: OutcomeStage; at: string }[];
  fit_score?: number | null;
  family?: string | null;
  /** Posting snapshots retained for document regeneration. */
  document_postings?: Record<string, JobPosting>;
  /** Posting and match details retained when this application was recorded. */
  application_result?: MatchResult | null;
}

/** Read-only outcome review (backend `calibration.OutcomeReview`). */
export interface OutcomePattern {
  kind: "dismissal_reason" | "dismissed_family" | "family_outcome";
  label: string;
  count: number;
  examples: string[];
  suggestion: string;
}

export interface OutcomeReview {
  applications: number;
  by_stage: Record<string, number>;
  high_fit_dismissals: number;
  patterns: OutcomePattern[];
  quiet: string[];
}

/** Your own call on a job (backend `labels.UserLabel`): measures the models, never filters. */
export type UserLabel = "yes" | "maybe" | "no";

/** One model setup's verdicts against your labels (backend `labels.SetupAgreement`). */
export interface SetupAgreement {
  models: string;
  yes: number;
  no: number;
  kept_yes: number;
  passed_no: number;
  agreement: number | null;
  ranking: number | null;
  misses: string[];
  false_accepts: string[];
}

/** Two setups on the same labelled jobs (backend `labels.HeadToHead`). */
export interface HeadToHead {
  first: SetupAgreement;
  second: SetupAgreement;
  shared: number;
  split: string[];
}

/** Backend `learning.Kind`: what a preference learned from your labels changes. */
export type PreferenceKind =
  | "requirement_gap"
  | "seniority_floor"
  | "not_a_fit"
  | "target_role"
  | "transferable_strength"
  | "adjacent_family";

/** Backend `learning.LearnedPreference`. */
export interface LearnedPreference {
  id: string;
  kind: PreferenceKind;
  text: string;
  /** The model's wording, when you reworded it before accepting. */
  proposed: string;
  rationale: string;
  label_ids: string[];
  family: RoleFamily | null;
  status: "pending" | "accepted" | "rejected";
  /** Accepted automatically from new labels or applied / N/A reasons. */
  auto: boolean;
  created_at: string;
  decided_at: string | null;
}

/** Backend `learning.LearningState`. */
export interface LearningState {
  pending: LearnedPreference[];
  accepted: LearnedPreference[];
  rejected: number;
  set_aside: string[];
}

/** Backend `labels.LabelReview`. */
export interface LabelReview {
  total: number;
  yes: number;
  maybe: number;
  no: number;
  target_total: number;
  target_each: number;
  ready: boolean;
  setups: SetupAgreement[];
  head_to_head: HeadToHead[];
  by_job: Record<string, UserLabel>;
}

/** Role-family results over the logged searches (backend `history.FamilyYield`). */
export interface FamilyYield {
  family: string;
  tier: string;
  searches: number;
  found: number;
  matches: number;
  /** false: no match after 3 searches; null: too early to tell. */
  landing: boolean | null;
}

/** Postings and true matches one source produced over the logged searches. */
export interface SourceYield {
  source: string;
  searches: number;
  found: number;
  matches: number;
  last_match: string | null;
}

export interface SkillEvidence {
  skill: string;
  level: "expert" | "proficient" | "familiar";
  evidence: string;
}

export type FamilyTier = "core" | "progression" | "adjacent";

/** A group of equivalent roles to search, with the CV evidence that makes it realistic. */
export interface RoleFamily {
  name: string;
  tier: FamilyTier;
  titles: string[];
  domain_terms: string[];
  /** Master CV ids (bullets, roles, projects) proving the work transfers. */
  evidence: string[];
  gap: string;
  rationale: string;
  /** The user asked to explore this area (career intent). */
  requested: boolean;
  /** Set by code: why it is not searched. */
  rejected: string | null;
}

export type LanguageLevel = "basic" | "conversational" | "professional" | "native";

export interface LanguageSkill {
  language: string;
  level: LanguageLevel;
}

/** What the user wants from the next role, kept apart from CV facts (backend `SearchIntent`). */
export interface SearchIntent {
  direction: string;
  energising_work: string[];
  avoid_work: string[];
  target_areas: string[];
  preferred_sectors: string[];
  avoided_sectors: string[];
  organisation_types: string[];
  soft_dealbreakers: string[];
  languages: LanguageSkill[];
  eligibility: string[];
  updated_at: string | null;
}

/** A proposed Master CV addition waiting for the user (backend `enrichment.QueuedEvidence`). */
export interface QueuedEvidence {
  id: string;
  owner: string;
  source: string;
  kind: "skill" | "certification" | "project" | "bullet";
  text: string;
  name: string | null;
  attach_to: string | null;
  quote: string;
  confidence: "high" | "medium" | "low";
  status: "pending" | "accepted" | "rejected";
  created_at: string;
}

/** What an ATS reads from the exported .docx. */
export interface ATSReport {
  words: number;
  est_pages: number;
  contact_missing: string[];
  keyword_coverage: number;
  missing_keywords: string[];
  warnings: string[];
}

export interface TailoredCVView {
  id: string;
  job_id: string;
  job_title: string;
  job_company: string;
  created_at: string;
  updated_at: string;
  template: string;
  download_url: string;
  cv: {
    basics: { name: string; headline: string | null; summary: string | null; email: string | null; phone: string | null; location: string | null };
    experience: { id: string; title: string; company: string; start: string; end: string | null; bullets: { id: string; text: string }[] }[];
    skills: { category: string; items: string[] }[];
    education: { institution: string; degree: string; field: string | null }[];
    certifications: { name: string; issuer: string | null }[];
    projects: { id: string; name: string; description: string; skills: string[] }[];
    languages: string[];
  };
  ats: ATSReport | null;
  source_ats_keyword_coverage: number | null;
  reviewed: boolean;
  imported: boolean;
}

/** What the candidate's publications (and patents, grants, talks, awards) show employers. */
export interface PublicationRecord {
  count: number;
  lead_author: number;
  years: string;
  themes: string[];
  notable: string[];
  other_outputs: string[];
  signals: string[];
  summary: string;
}

export interface ProfileSummary {
  headline: string;
  seniority: string;
  years_experience: number;
  core_expertise: string[];
  key_skills: SkillEvidence[];
  domains: string[];
  leadership: string[];
  qualifications: string[];
  achievements: string[];
  transferable_strengths: string[];
  /** Every capability the CV evidences (technical, delivery, leadership, commercial, communication). */
  capabilities: string[];
  publications?: PublicationRecord | null;
  target_roles: string[];
  stretch_roles: string[];
  not_a_fit: string[];
  search_keywords: string[];
  summary: string;
  role_families?: RoleFamily[];
}

export interface ProfileRecord {
  key: string;
  cv_fingerprint: string;
  cv_id: string | null;
  /** File name of the CV the profile was built from. */
  cv_name: string | null;
  /** The CV was edited after this profile was built; it is only rebuilt when the user asks. */
  cv_changed?: boolean;
  /** The career intent changed after this profile was built. */
  intent_changed?: boolean;
  role_family: string;
  created_at: string;
  summary: ProfileSummary;
  edited: boolean;
  updated_at: string | null;
}

export interface ProgressEvent {
  stage: string;
  message: string;
  source?: string;
  status?: string;
  done?: number;
  total?: number;
  /** "review" = the second opinions, a live row of their own after the first pass. */
  phase?: string;
  at: number;
}

export type ModelUsageEvent = Extract<ChatEvent, { type: "model_usage" }>;

/** Live progress of a profile build or a document being written (`GET /api/progress/{id}`). */
export interface TaskProgress {
  id: string;
  title: string;
  step: string;
  done: number;
  total: number;
  started_at: number;
  step_started_at: number;
  updated_at: number;
  /** The model call in flight, e.g. "CV tailoring (claude-opus-5-5)". */
  waiting_on: string | null;
  waiting_since: number | null;
  completed: string[];
  finished: boolean;
  error: string | null;
  /** Server clock at snapshot time (seconds), for elapsed times. */
  now: number;
}

/** Sent by a search stream after a few quiet seconds: the server is alive and waiting on this. */
export interface Heartbeat {
  waiting_on: string | null;
  waiting_s?: number;
  at: number;
}

export type SearchStreamEvent =
  | ({ type: "search_progress" } & Omit<ProgressEvent, "at">)
  | ({ type: "heartbeat" } & Omit<Heartbeat, "at">)
  | ModelUsageEvent
  | { type: "search_results"; outcome: SearchOutcome }
  | { type: "error"; message: string };

export interface ClaudeLimitWindow {
  window: string;
  status: string | null;
  resets_at: number | null;
  utilization: number | null;
  using_overage: boolean;
  observed_at: number | null;
}

export interface ClaudeCodeUsage {
  windows: ClaudeLimitWindow[];
  updated_at: number | null;
}

export interface MatchReport {
  threshold: number;
  matches: MatchResult[];
  /** Would match on what could be read, but the full posting (its requirements) was not read yet. */
  to_check?: MatchResult[];
  below_threshold: MatchResult[];
  excluded: MatchResult[];
  /** Already applied for: set aside before screening, never ranked. */
  applied?: MatchResult[];
  /** Marked N/A by the user: set aside before screening. */
  dismissed?: MatchResult[];
  not_retrieved: string[];
  screened: boolean;
  summary: ProfileSummary | null;
}

export interface SourceReport {
  name: string;
  status: "used" | "failed" | "skipped";
  fetched: number;
  detail: string | null;
}

/** A past search as listed in the history (results are loaded on open). */
export interface HistoryItem {
  id: string;
  created_at: string;
  label: string;
  query: SearchQuery;
  fetched: number;
  matches: number;
  screened: boolean;
  /** Provider, models and efforts (when the AI matcher ran). */
  models: string | null;
}

export interface SearchOutcome {
  report: MatchReport;
  fetched: number;
  alert_only: boolean;
  sources: SourceReport[];
  cv_titles: string[];
  errors: Record<string, string>;
  skipped_sources: Record<string, string>;
  summary_from_memory: boolean | null;
  smart_unavailable: string | null;
  seconds: number;
  /** The user stopped the search; what was done is kept. */
  cancelled: boolean;
  /** Shortlisted jobs not judged yet (stopped or failed batches): Continue judges them. */
  unscreened: number;
  history_id: string | null;
  /** The progress lines, with seconds since the start (saved with the search). */
  progress_log?: string[];
  /** Role families searched: name -> tier. */
  families?: Record<string, FamilyTier>;
}

export interface LLMView {
  provider: Provider;
  /** CV and letters, second opinions, the assistant. */
  quality_model: string;
  /** First-pass job matching. */
  screening_model: string;
  quality_effort: Effort;
  screening_effort: Effort;
  /** The profile summary; empty means the quality model (and its effort) builds it. */
  profile_model: string;
  profile_effort: Effort;
  /** The profile model's own provider (null: `provider`). Used only with a profile model. */
  profile_provider: Provider | null;
  key_set: boolean;
  ready: boolean;
  /** Credentials available for the provider that builds the profile. */
  profile_ready: boolean;
}

export interface CVAsset {
  id: string;
  filename: string;
  size: number;
  kind: "master" | "uploaded" | "generated";
  parsed: boolean;
  selected: boolean;
}

export interface MasterCV {
  schema_version: string;
  basics: {
    name: string;
    headline: string | null;
    email: string | null;
    phone: string | null;
    location: string | null;
    links: { label: string; url: string }[];
    summary: string | null;
  };
  experience: {
    id: string;
    company: string;
    title: string;
    location: string | null;
    start: string;
    end: string | null;
    bullets: { id: string; text: string; skills: string[]; metrics: string[] }[];
  }[];
  education: {
    institution: string;
    degree: string;
    field: string | null;
    start: string | null;
    end: string | null;
    details: string[];
  }[];
  skills: { category: string; items: string[] }[];
  certifications: { name: string; issuer: string | null; year: number | null }[];
  projects: { id: string; name: string; description: string; skills: string[]; url: string | null }[];
  languages: string[];
  preferences: {
    target_titles: string[];
    locations: string[];
    work_arrangements: WorkArrangement[];
    willing_to_relocate: boolean;
    min_salary: number | null;
  };
}

export interface CoverLetterView {
  id: string;
  title: string;
  company: string;
  candidate_name: string;
  created_at: string;
  updated_at: string;
  filename: string;
  greeting: string;
  paragraphs: string[];
  closing: string;
  docx_url: string;
  txt_url: string;
}

export interface AppState {
  llm: LLMView;
  cv: { name: string; headline: string | null; roles: number; skills: number } | null;
  cv_files: { available: CVAsset[]; selected: string | null };
  sources: { available: string[]; skipped: Record<string, string>; catalog: SourceInfo[] };
  defaults: { threshold: number };
}

/** Events streamed over /api/ws/chat. */
export type ChatEvent =
  | { type: "session"; session_id: string; history: { role: "user" | "assistant"; text: string }[] }
  | { type: "agent_status"; agent: string; depth: number; status: string }
  | {
      type: "model_usage";
      agent: string;
      depth: number;
      model: string;
      provider?: Provider;
      input_tokens: number;
      output_tokens: number;
      estimated: boolean;
      purpose: string;
    }
  | { type: "agent_message"; agent: string; depth: number; text: string; final: boolean }
  | { type: "tool_call"; agent: string; depth: number; id: string; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; agent: string; depth: number; id: string; tool: string; ok: boolean; preview: string }
  | { type: "cv_updated"; reason: string }
  | { type: "profile_updated"; key: string; reason: string }
  | { type: "intent_updated"; intent: SearchIntent; changes: string[] }
  | { type: "evidence_proposed"; count: number }
  | { type: "search_progress"; stage: string; message: string }
  | { type: "search_filters_updated"; query: SearchQuery }
  | { type: "search_results"; outcome: SearchOutcome }
  | { type: "saved_updated"; count: number }
  | { type: "tracking_updated"; job_id: string; tracking: JobTracking }
  | { type: "tracker_updated" }
  | { type: "label_updated"; job_id: string; label: "yes" | "maybe" | "no" | null }
  | { type: "documents_updated"; job_id?: string }
  | { type: "cv_selected"; id: string }
  | { type: "learning_updated" }
  | { type: "companies_updated" }
  | { type: "history_updated" }
  | { type: "result_removed"; job_id: string }
  | { type: "models_updated" }
  | { type: "done"; tokens: { input: number; output: number }; cancelled?: boolean }
  | { type: "error"; message: string };
