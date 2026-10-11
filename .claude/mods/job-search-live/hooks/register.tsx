import { atom, read, update } from 'claude-code'
import type { EngineInterface, ModelUsage, Register, SessionContextUsage, SessionRateLimit } from 'claude-code'

import type { AgentRow, AppTask, ClaudeLimit, Gate, LiveStatus, MainThread, Plan, SessionTokens } from '../types'

// A thin view over `scripts/live_status.py` (one JSON line): the app's running searches and
// tasks with the tokens each has used, the quality gate, plan windows; plus this session's own
// main thread, subagents and tokens. The logic lives in the project's scripts, so the app, this
// pane and a terminal agree.
//
// Claude plan windows come from this session's engine (`session.measure`), which states the
// share used at any level; the `claude -p` calls the app makes state it only near a limit, so
// the pane passes each reading on to the app (`live_status.py claude-limits`). The plans give
// no token allowance, only a share: "left" is a percentage, and next to it are the tokens the
// app itself used inside that window. No prices: the plans are paid by subscription.

const PANE = 'job-search-live'
const TITLE = 'Job search'
const POLL_MS = 3000
const PLANS_EVERY = 10 // polls between plan reads (they ask the backend for /api/connections)
const KEEP_AGENTS = 12
const SPIN = ['◐', '◓', '◑', '◒']
type Usage = ModelUsage
type RawLimit = SessionRateLimit
type Context = SessionContextUsage
const CV_SKILLS = new Set(['cv_writer'])
const KINDS: [RegExp, string][] = [
  [/explore/i, 'explorer'],
  [/plan/i, 'planner'],
  [/review|critic|verif/i, 'reviewer'],
  [/test/i, 'tester'],
  [/guide/i, 'guide'],
]

const NO_TOKENS: SessionTokens = {
  input: 0,
  cacheRead: 0,
  cacheWrite: 0,
  output: 0,
  requests: 0,
  subagents: 0,
  contextPercent: null,
  contextTokens: null,
  contextWindow: null,
}

const status = atom({ plugin: 'job-search-live', key: 'status' } as const, null as LiveStatus | null)
const plans = atom({ plugin: 'job-search-live', key: 'plans' } as const, [] as Plan[])
const agents = atom({ plugin: 'job-search-live', key: 'agents' } as const, [] as AgentRow[])
const main = atom({ plugin: 'job-search-live', key: 'main' } as const, {
  running: false,
  started: 0,
  calls: 0,
  last: '',
} as MainThread)
const frame = atom({ plugin: 'job-search-live', key: 'frame' } as const, 0)
const claude = atom({ plugin: 'job-search-live', key: 'claude' } as const, [] as ClaudeLimit[])
const session = atom({ plugin: 'job-search-live', key: 'session' } as const, NO_TOKENS)

let off: boolean | null = null
let polls = 0
let pushed = '' // the Claude readings the app last accepted
let spinner: { cancel: () => void } | null = null
let mainTurn: string | null = null // the user's running turn

// ------------------------------------------------------------------ pure helpers (tested)

export function kindOf(subagentType: string, description: string): string {
  const text = `${subagentType} ${description}`
  return KINDS.find(([rx]) => rx.test(text))?.[1] ?? (subagentType || 'agent')
}

export function parseStatus(stdout: string): LiveStatus | null {
  try {
    const data = JSON.parse(stdout.trim().split('\n').pop() ?? '') as LiveStatus
    return typeof data === 'object' && data !== null && 'backend' in data ? data : null
  } catch {
    return null
  }
}

/** A task's steps as `● done ─ ◐ current ─ ○ pending`. */
export function stepper(task: AppTask, spin: string): string {
  const done = task.completed.map(s => `● ${s}`)
  const now = task.step ? [`${spin} ${task.step}`] : []
  const left = Math.max(0, task.total - task.completed.length - now.length)
  return [...done, ...now, ...Array.from({ length: Math.min(left, 4) }, () => '○')].join(' ─ ')
}

export function gateLine(g: Gate | null): string {
  if (g === null) return 'gate not run yet'
  return g.checks.map(c => `${c.ok ? '✓' : '✗'} ${c.name}`).join(' · ')
}

/** A token count as `850`, `12.3k` or `1.2M`. */
export function tok(n: number): string {
  if (n < 1000) return String(Math.round(n))
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
  return `${(n / 1_000_000).toFixed(1)}M`
}

/** `five_hour` -> `5h`, `seven_day_opus` -> `7d opus`; app names (`5h`) pass through. */
export function windowName(kind: string): string {
  const m = /^(five_hour|seven_day)(?:_(\w+))?$/.exec(kind)
  if (!m) return kind
  return `${m[1] === 'five_hour' ? '5h' : '7d'}${m[2] ? ` ${m[2]}` : ''}`
}

/** A window's share as `40% used · 60% left · resets 14:30 · app 5.4k tok`. */
export function windowText(used: number | null, resetsAt: number | null, appTokens?: number | null): string {
  const parts = [used === null ? 'within limit' : `${Math.round(used)}% used · ${Math.max(0, 100 - Math.round(used))}% left`]
  if (resetsAt) parts.push(`resets ${clock(resetsAt)}`)
  if (appTokens) parts.push(`app ${tok(appTokens)} tok`)
  return parts.join(' · ')
}

/** A plan's first line: its label, then today's app tokens or when a spent plan returns. */
export function planLine(p: Plan): string {
  if (p.exhausted_until) return `${p.label} spent until ${clock(p.exhausted_until)}`
  const today = p.tokens_today ? ` · today ${tok(p.tokens_today)} tok in ${p.calls_today ?? 0} call${p.calls_today === 1 ? '' : 's'}` : ''
  const wins = p.windows.length ? '' : ' within limits'
  return `${p.label}${wins}${today}`
}

/** The Claude plan's windows as the engine reports them, with the app's tokens in each. */
export function claudeWindows(limits: ClaudeLimit[], app: Plan | undefined): { name: string; used: number; text: string }[] {
  return limits.map(l => {
    const name = windowName(l.kind)
    const mine = app?.windows.find(w => w.name === name)
    return { name, used: l.percentUsed, text: windowText(l.percentUsed, l.resetsAt, mine?.tokens) }
  })
}

/** An app task's model use so far: `3 calls · 9.8k tok` (`~` when partly estimated). */
export function taskTokens(t: AppTask): string {
  if (!t.calls) return ''
  const total = (t.input_tokens ?? 0) + (t.output_tokens ?? 0)
  return `${t.calls} call${t.calls === 1 ? '' : 's'} · ${t.tokens_estimated ? '~' : ''}${tok(total)} tok`
}

/** This session's tokens: `in 1.2M (cache 1.1M) · out 45k · 132 requests`. */
export function sessionLine(s: SessionTokens): string {
  if (s.requests === 0) return 'no model requests yet'
  const input = s.input + s.cacheRead + s.cacheWrite
  const sub = s.subagents ? ` · subagents ${tok(s.subagents)}` : ''
  return `in ${tok(input)} (cache ${tok(s.cacheRead + s.cacheWrite)}) · out ${tok(s.output)} · ${s.requests} request${s.requests === 1 ? '' : 's'}${sub}`
}

/** A 10-cell bar for a share used. */
export function bar(used: number | null): string {
  if (used === null) return '··········'
  const full = Math.max(0, Math.min(10, Math.round(used / 10)))
  return '█'.repeat(full) + '░'.repeat(10 - full)
}

/** The engine's rate-limit windows as plain values (ISO reset -> epoch seconds). */
export function readLimits(raw: readonly { kind: string; percentUsed: number; resetsAt?: string }[]): ClaudeLimit[] {
  return raw.map(r => {
    const at = r.resetsAt ? Date.parse(r.resetsAt) : NaN
    return { kind: r.kind, percentUsed: r.percentUsed, resetsAt: Number.isFinite(at) ? Math.round(at / 1000) : null }
  })
}

function clock(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function elapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

function summary(e: Record<string, unknown>): string {
  for (const key of ['description', 'command', 'file_path', 'pattern', 'query', 'skill']) {
    const v = e[key]
    if (typeof v === 'string' && v) return v.replace(/\s+/g, ' ').slice(0, 80)
  }
  return ''
}

// ------------------------------------------------------------------ engine helpers

async function isOff($: EngineInterface): Promise<boolean> {
  if (off !== null) return off
  // The desktop app does these jobs itself; a workspace without the script has nothing to show.
  const app = (await $.env.get('JOB_SEARCH_APP')) === '1'
  const root = await $.session.root()
  const script = await $.fs.read(`${root}/scripts/live_status.py`).catch(() => null)
  off = app || typeof script !== 'string'
  return off
}

async function poll($: EngineInterface) {
  const withPlans = polls % PLANS_EVERY === 0
  polls += 1
  const root = await $.session.root()
  // The session's own figures every tick (the plain call costs nothing): `session.measure`
  // fires only after a turn or when a window moves a whole point, so a long turn would
  // otherwise leave the windows and the context fill where they were.
  const now = await $.session.usage().catch(() => null)
  if (now) await measure($, now.rateLimits, now.context)
  await pushClaude($, withPlans) // before the plans read below, so it already has them
  const argv = ['python3', 'scripts/live_status.py', 'status', ...(withPlans ? ['--plans'] : [])]
  const ran = await $.process.run(argv, { cwd: root, timeoutMs: 15_000 }).catch(() => null)
  const parsed = ran ? parseStatus(ran.stdout) : null
  if (parsed === null) return
  await update($, status, () => parsed)
  if (parsed.plans) await update($, plans, () => parsed.plans ?? [])
  await showStatus($)
}

/** Pass this session's Claude windows on to the app (see the header) when they changed, or
 *  always with `force` (a restarted app has none); never throws. */
async function pushClaude($: EngineInterface, force = false) {
  const limits = await read($, claude)
  if (limits.length === 0) return
  const body = JSON.stringify(limits.map(l => ({ kind: l.kind, percent_used: l.percentUsed, resets_at: l.resetsAt })))
  if (body === pushed && !force) return
  if ((await read($, status))?.backend === false) return // the app is down: nothing to tell
  const root = await $.session.root()
  const ran = await $.process
    .run(['python3', 'scripts/live_status.py', 'claude-limits', body], { cwd: root, timeoutMs: 10_000 })
    .catch(() => null)
  if (ran !== null && /"ok":\s*true/.test(ran.stdout)) pushed = body // else: tried again next tick
}

/** Count one model request of this session: totals, and the main turn's or its subagent's row. */
async function countRequest($: EngineInterface, u: Usage, agentId: string | undefined) {
  const total = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens + u.output_tokens
  await update($, session, s => ({
    ...s,
    input: s.input + u.input_tokens,
    cacheRead: s.cacheRead + u.cache_read_input_tokens,
    cacheWrite: s.cacheWrite + u.cache_creation_input_tokens,
    output: s.output + u.output_tokens,
    requests: s.requests + 1,
    subagents: s.subagents + (agentId ? total : 0),
  }))
  if (agentId === undefined) {
    await update($, main, m => ({ ...m, tokens: (m.tokens ?? 0) + total }))
    return
  }
  // A subagent's requests name its loop id; its row was keyed by the Agent call. Match once by
  // the description the engine lists for that id.
  let list = await read($, agents)
  let row = list.find(a => a.agentId === agentId)
  if (row === undefined) {
    const info = (await $.agent.list().catch(() => [])).find(a => a.id === agentId)
    row = list.find(a => a.agentId === undefined && a.status === 'running' && (!info || a.description === info.description))
  }
  if (row === undefined) return
  const id = row.id
  await update($, agents, all => all.map(a => (a.id === id ? { ...a, agentId, tokens: (a.tokens ?? 0) + total } : a)))
}

async function showStatus($: EngineInterface) {
  const s = await read($, status)
  if (s === null) return
  const work = s.searches + s.tasks.length
  const gate = s.gate === null ? '' : s.gate.ok ? ' · gate ✓' : ' · gate ✗'
  const p = await read($, plans)
  const spent = p.filter(one => one.exhausted_until).map(one => ` · ${one.label} spent`)
  const session5h = (await read($, claude)).find(l => l.kind === 'five_hour')
  const codex5h = p.find(one => one.id === 'codex' && !one.exhausted_until)?.windows.find(w => w.name === '5h')
  const left = [
    session5h ? ` · Claude 5h ${Math.max(0, 100 - Math.round(session5h.percentUsed))}% left` : '',
    codex5h?.used_percent != null ? ` · Codex 5h ${Math.max(0, 100 - Math.round(codex5h.used_percent))}% left` : '',
  ].join('')
  $.ui.status(`app ${s.backend ? '●' : '○'}${work ? ` · ${work} running` : ''}${gate}${spent.join('')}${left}`)
}

async function measure($: EngineInterface, limits: readonly RawLimit[], context: Context) {
  await update($, claude, () => readLimits(limits))
  await update($, session, s => ({
    ...s,
    contextPercent: context.percent ?? null,
    contextTokens: context.tokens ?? null,
    contextWindow: context.window,
  }))
}

async function openPane($: EngineInterface) {
  await $.ui.open({ id: PANE, title: TITLE })
}

// ------------------------------------------------------------------ hooks

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    if (await isOff($)) return next(e)
    await $.command.register({ name: 'live', description: 'Show the job-search app, gate, plans and agents in a pane' })
    await $.command.register({ name: 'gate', description: 'Run the full quality gate (ruff, mypy, tsc, pytest)' })
    if (options.pane === 'auto') void openPane($)
    const now = await $.session.usage().catch(() => null)
    if (now) await measure($, now.rateLimits, now.context)
    void poll($)
    $.clock.every(POLL_MS, () => poll($))

    return next(e)
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'live' }, async $ => {
    await poll($)
    await openPane($)

    return { text: 'Job-search pane opened.' }
  })

  on('command.run', { command: 'gate' }, async $ => {
    const root = await $.session.root()
    const ran = await $.process.run(['python3', '.claude/hooks/gate.py', '--all'], { cwd: root, timeoutMs: 600_000 })
    await poll($)
    const s = await read($, status)
    if (ran.exitCode === 0) return { text: `Quality gate passed: ${gateLine(s?.gate ?? null)}` }
    const tail = (ran.stderr || ran.stdout).trim().split('\n').slice(-25).join('\n')

    return { text: `Quality gate failed: ${gateLine(s?.gate ?? null)}\n\n${tail}` }
  })

  // The Claude plan's windows and the context fill, pushed after each turn and whenever a
  // window moves a whole point.
  on('session.measure', async ($, e, next) => {
    if (await isOff($)) return next(e)
    await measure($, e.rateLimits, e.context)
    if (e.changed.includes('rateLimits')) void pushClaude($, true)
    await showStatus($)

    return next(e)
  }).catch(($, e, next) => next(e))

  // Every model request, main or subagent, as it completes: tokens count live, mid-turn.
  on('turn.step', async function* ($, e, next) {
    const r = yield* next(e)
    try {
      if (r.usage && !(await isOff($))) await countRequest($, r.usage, e.agentId)
    } catch {
      // counting is advisory: never disturb the turn
    }

    return r
  })

  on('turn.start', async ($, e, next) => {
    if (await isOff($)) return next(e)
    mainTurn = e.turnId
    const now = await $.clock.now()
    await update($, main, m => (m.running ? m : { running: true, started: now, calls: 0, last: '', tokens: 0 }))
    if (spinner === null) spinner = $.clock.every(400, () => update($, frame, f => (f + 1) % SPIN.length))
    if (options.pane === 'auto' && !(await $.ui.panes()).some(p => p.id === PANE)) void openPane($)

    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const ran = await next(e)
    if (e.turnId === mainTurn) {
      mainTurn = null
      await update($, main, m => ({ ...m, running: false }))
      spinner?.cancel()
      spinner = null
    }

    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', async ($, e, next) => {
    if (await isOff($)) return next(e)
    const input = e as unknown as Record<string, unknown>
    if (e.agentId === undefined) {
      await update($, main, m => ({ ...m, calls: m.calls + 1, last: `${String(e.tool)} ${summary(input)}`.trim() }))
    }
    if (e.tool !== 'Agent') return next(e)
    const row: AgentRow = {
      id: e.tool_use_id,
      kind: kindOf(String(input.subagent_type ?? ''), String(input.description ?? '')),
      description: String(input.description ?? ''),
      started: await $.clock.now(),
      ended: null,
      status: 'running',
    }
    await update($, agents, list => [...list, row].slice(-KEEP_AGENTS))
    const ran = await next(e)
    const failed = 'deny' in ran && typeof ran.deny === 'string' ? true : Boolean(ran.isError)
    const ended = await $.clock.now()
    await update($, agents, list =>
      list.map(a => (a.id === row.id ? { ...a, ended, status: failed ? ('failed' as const) : ('done' as const) } : a)),
    )

    return ran
  }).catch(($, e, next) => next(e))

  // Tailoring reads the Master CV: check it still validates before the skill runs.
  on('skill.prompt', async ($, e, next) => {
    if (!CV_SKILLS.has(e.skill) || (await isOff($))) return next(e)
    const root = await $.session.root()
    const checked = await $.process.run(['python3', '.claude/hooks/gate.py', '--validate-cv'], {
      cwd: root,
      timeoutMs: 60_000,
    })
    if (checked.exitCode === 0) return next(e)
    const said = checked.stdout.trim().split('\n').slice(0, 8).join('\n')
    const notice =
      checked.exitCode === 1
        ? 'job-search-live checked data/master_cv.json before this skill: it does not validate. ' +
          'Stop. Tell the user which fields fail (below) and ask them to fix the CV in the app ' +
          'before you tailor anything. Do not edit data/master_cv.json yourself.'
        : 'job-search-live could not tell whether data/master_cv.json validates ' +
          `(exit ${checked.exitCode}). Say so to the user before relying on it.`
    if (checked.exitCode === 1) $.ui.toast('The Master CV does not validate: fix it before tailoring.')

    return next({ ...e, text: `${notice}\n\n${said}\n\n${e.text}` })
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, status)
    const p = await read($, plans)
    const list = await read($, agents)
    const m = await read($, main)
    const limits = await read($, claude)
    const tokens = await read($, session)
    const spin = SPIN[(await read($, frame)) % SPIN.length] ?? '◐'
    const claudeApp = p.find(one => one.id === 'claude_code')
    const shown = claudeWindows(limits, claudeApp)
    const tone = (used: number | null) => (used === null ? undefined : used >= 90 ? 'error' : used >= 75 ? 'warning' : 'success')
    const now = await $.clock.now()
    const width = Math.max(20, e.props.bodyColumns ?? 60)

    return (
      <Box flexDirection="column">
        <Text bold>App</Text>
        {s === null ? (
          <Text dimColor>Waiting for scripts/live_status.py…</Text>
        ) : (
          <Text>
            <Text color={s.backend ? 'success' : 'error'}>{s.backend ? '●' : '○'}</Text> backend{' '}
            <Text color={s.frontend ? 'success' : 'error'}>{s.frontend ? '●' : '○'}</Text> frontend
            <Text dimColor> · {s.git_dirty} changed path{s.git_dirty === 1 ? '' : 's'}</Text>
          </Text>
        )}
        {s !== null && s.searches > 0 && <Text color="claude">{spin} {s.searches} search{s.searches === 1 ? '' : 'es'} running</Text>}
        {s?.tasks.map(t => (
          <Box flexDirection="column" marginTop={1}>
            <Text wrap="truncate-end">
              {t.title}
              {taskTokens(t) && <Text dimColor> · {taskTokens(t)}</Text>}
            </Text>
            <Text wrap="truncate-end">{stepper(t, spin).slice(0, width * 2)}</Text>
            {t.waiting_on && <Text dimColor wrap="truncate-end">waiting for {t.waiting_on}</Text>}
          </Box>
        ))}
        {s !== null && s.searches === 0 && s.tasks.length === 0 && <Text dimColor>No app work running.</Text>}

        <Text bold>{'\n'}Gate</Text>
        <Text color={s?.gate === null || s?.gate === undefined ? undefined : s.gate.ok ? 'success' : 'error'} wrap="truncate-end">
          {gateLine(s?.gate ?? null)}
        </Text>

        {(p.length > 0 || shown.length > 0) && <Text bold>{'\n'}Plans</Text>}
        {shown.length > 0 && (
          <Text wrap="truncate-end">
            Claude <Text dimColor>(this account, live){claudeApp?.tokens_today ? ` · app today ${tok(claudeApp.tokens_today)} tok` : ''}</Text>
          </Text>
        )}
        {shown.map(w => (
          <Text wrap="truncate-end">
            {'  '}
            {w.name.padEnd(8)} <Text color={tone(w.used)}>{bar(w.used)}</Text> <Text dimColor>{w.text}</Text>
          </Text>
        ))}
        {p
          .filter(one => !(one.id === 'claude_code' && shown.length > 0))
          .map(one => (
            <Box flexDirection="column">
              <Text color={one.exhausted_until ? 'error' : undefined} wrap="truncate-end">
                {planLine(one)}
              </Text>
              {!one.exhausted_until &&
                one.windows.map(w => (
                  <Text wrap="truncate-end">
                    {'  '}
                    {w.name.padEnd(8)} <Text color={tone(w.used_percent)}>{bar(w.used_percent)}</Text>{' '}
                    <Text dimColor>{windowText(w.used_percent, w.resets_at, w.tokens)}</Text>
                  </Text>
                ))}
            </Box>
          ))}

        <Text bold>{'\n'}This session</Text>
        <Text color={m.running ? 'claude' : undefined} dimColor={!m.running} wrap="truncate-end">
          {m.running
            ? `${spin} working ${elapsed(now - m.started)} · ${m.calls} tool call${m.calls === 1 ? '' : 's'}${m.tokens ? ` · ${tok(m.tokens)} tok` : ''}${m.last ? ` · ${m.last}` : ''}`
            : 'idle'}
        </Text>
        <Text dimColor wrap="truncate-end">
          tokens {sessionLine(tokens)}
        </Text>
        {tokens.contextPercent !== null && (
          <Text dimColor wrap="truncate-end">
            context {tokens.contextPercent}%{tokens.contextTokens !== null && tokens.contextWindow ? ` (${tok(tokens.contextTokens)} of ${tok(tokens.contextWindow)})` : ''}
          </Text>
        )}
        {list.length === 0 && <Text dimColor>No subagents yet.</Text>}
        {list.map(a => (
          <Text dimColor={a.status !== 'running'} color={a.status === 'failed' ? 'error' : undefined} wrap="truncate-end">
            {a.status === 'running' ? spin : a.status === 'done' ? '✓' : '✗'} {a.kind.padEnd(9)}{' '}
            {elapsed((a.ended ?? now) - a.started).padStart(6)}
            {a.tokens ? ` ${tok(a.tokens).padStart(6)} tok` : ''} {a.description}
          </Text>
        ))}
      </Box>
    )
  })
}
