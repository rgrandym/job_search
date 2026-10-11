import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRow, AppTask, Gate, LiveStatus, MainThread, Plan } from '../types'

// A thin view over `scripts/live_status.py` (one JSON line): the app's running searches and
// tasks, the quality gate, plan windows; plus this session's own main thread and subagents.
// The logic lives in the project's scripts, so the app, this pane and a terminal agree.

const PANE = 'job-search-live'
const TITLE = 'Job search'
const POLL_MS = 3000
const PLANS_EVERY = 10 // polls between plan reads (they ask the backend for /api/connections)
const KEEP_AGENTS = 12
const SPIN = ['◐', '◓', '◑', '◒']
const CV_SKILLS = new Set(['cv_writer'])
const KINDS: [RegExp, string][] = [
  [/explore/i, 'explorer'],
  [/plan/i, 'planner'],
  [/review|critic|verif/i, 'reviewer'],
  [/test/i, 'tester'],
  [/guide/i, 'guide'],
]

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

let off: boolean | null = null
let polls = 0
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

export function planLine(p: Plan): string {
  if (p.exhausted_until) return `${p.label} spent until ${clock(p.exhausted_until)}`
  const wins = p.windows.filter(w => w.used_percent !== null).map(w => `${w.name} ${Math.round(w.used_percent ?? 0)}%`)
  return `${p.label} ${wins.length ? wins.join(' · ') : 'within limits'}`
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
  const argv = ['python3', 'scripts/live_status.py', 'status', ...(withPlans ? ['--plans'] : [])]
  const ran = await $.process.run(argv, { cwd: root, timeoutMs: 15_000 }).catch(() => null)
  const parsed = ran ? parseStatus(ran.stdout) : null
  if (parsed === null) return
  await update($, status, () => parsed)
  if (parsed.plans) await update($, plans, () => parsed.plans ?? [])
  await showStatus($)
}

async function showStatus($: EngineInterface) {
  const s = await read($, status)
  if (s === null) return
  const work = s.searches + s.tasks.length
  const gate = s.gate === null ? '' : s.gate.ok ? ' · gate ✓' : ' · gate ✗'
  const spent = (await read($, plans)).filter(p => p.exhausted_until).map(p => ` · ${p.label} spent`)
  $.ui.status(`app ${s.backend ? '●' : '○'}${work ? ` · ${work} running` : ''}${gate}${spent.join('')}`)
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

  on('turn.start', async ($, e, next) => {
    if (await isOff($)) return next(e)
    mainTurn = e.turnId
    const now = await $.clock.now()
    await update($, main, m => (m.running ? m : { running: true, started: now, calls: 0, last: '' }))
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
    const spin = SPIN[(await read($, frame)) % SPIN.length] ?? '◐'
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
            <Text>{t.title}</Text>
            <Text wrap="truncate-end">{stepper(t, spin).slice(0, width * 2)}</Text>
            {t.waiting_on && <Text dimColor wrap="truncate-end">waiting for {t.waiting_on}</Text>}
          </Box>
        ))}
        {s !== null && s.searches === 0 && s.tasks.length === 0 && <Text dimColor>No app work running.</Text>}

        <Text bold>{'\n'}Gate</Text>
        <Text color={s?.gate === null || s?.gate === undefined ? undefined : s.gate.ok ? 'success' : 'error'} wrap="truncate-end">
          {gateLine(s?.gate ?? null)}
        </Text>

        {p.length > 0 && <Text bold>{'\n'}Plans</Text>}
        {p.map(one => (
          <Text color={one.exhausted_until ? 'error' : undefined} dimColor={!one.exhausted_until} wrap="truncate-end">
            {planLine(one)}
          </Text>
        ))}

        <Text bold>{'\n'}This session</Text>
        <Text color={m.running ? 'claude' : undefined} dimColor={!m.running} wrap="truncate-end">
          {m.running
            ? `${spin} working ${elapsed(now - m.started)} · ${m.calls} tool call${m.calls === 1 ? '' : 's'}${m.last ? ` · ${m.last}` : ''}`
            : 'idle'}
        </Text>
        {list.length === 0 && <Text dimColor>No subagents yet.</Text>}
        {list.map(a => (
          <Text dimColor={a.status !== 'running'} color={a.status === 'failed' ? 'error' : undefined} wrap="truncate-end">
            {a.status === 'running' ? spin : a.status === 'done' ? '✓' : '✗'} {a.kind.padEnd(9)}{' '}
            {elapsed((a.ended ?? now) - a.started).padStart(6)} {a.description}
          </Text>
        ))}
      </Box>
    )
  })
}
