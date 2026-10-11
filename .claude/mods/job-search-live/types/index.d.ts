/** One line of `python3 scripts/live_status.py status` (see that script's docstring). */
export type AppTask = {
  title: string
  step: string
  done: number
  total: number
  completed: string[]
  waiting_on: string | null
}
export type PlanWindow = { name: string; used_percent: number | null; resets_at: number | null }
export type Plan = { label: string; windows: PlanWindow[]; exhausted_until: number | null }
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
}
export type MainThread = { running: boolean; started: number; calls: number; last: string }

declare module 'claude-code' {
  interface PluginState {
    'job-search-live': {
      status: LiveStatus | null
      plans: Plan[]
      agents: AgentRow[]
      main: MainThread
      frame: number
    }
  }
}
