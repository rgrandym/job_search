/** One line of `python3 scripts/live_status.py status` (see that script's docstring). */
export type AppTask = {
  title: string
  step: string
  done: number
  total: number
  completed: string[]
  waiting_on: string | null
  calls?: number
  input_tokens?: number
  output_tokens?: number
  tokens_estimated?: boolean
}
/** `tokens`: the app's own tokens inside the window so far. */
export type PlanWindow = { name: string; used_percent: number | null; resets_at: number | null; tokens?: number | null }
export type Plan = {
  id?: string
  label: string
  windows: PlanWindow[]
  exhausted_until: number | null
  tokens_today?: number
  calls_today?: number
}
export type Gate = { ok: boolean; kind: string; at: number; checks: { name: string; ok: boolean }[] }
export type LiveStatus = {
  backend: boolean
  frontend: boolean
  searches: number
  tasks: AppTask[]
  gate: Gate | null
  git_dirty: number
  plans?: Plan[]
}
export type AgentRow = {
  id: string
  kind: string
  description: string
  started: number
  ended: number | null
  status: 'running' | 'done' | 'failed'
  /** The subagent's loop id, once one of its requests named it. */
  agentId?: string
  tokens?: number
}
export type MainThread = { running: boolean; started: number; calls: number; last: string; tokens?: number }
/** A Claude plan window as this session's engine reports it (`session.measure`). */
export type ClaudeLimit = { kind: string; percentUsed: number; resetsAt: number | null }
/** This Claude Code session's tokens, summed over every model request (main and subagents). */
export type SessionTokens = {
  input: number
  cacheRead: number
  cacheWrite: number
  output: number
  requests: number
  subagents: number
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'job-search-live': {
      status: LiveStatus | null
      plans: Plan[]
      agents: AgentRow[]
      main: MainThread
      frame: number
      claude: ClaudeLimit[]
      session: SessionTokens
    }
  }
}
