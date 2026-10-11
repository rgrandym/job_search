import { describe, expect, test } from 'claude-code/testing'

import {
  bar,
  claudeWindows,
  gateLine,
  kindOf,
  parseStatus,
  planLine,
  readLimits,
  sessionLine,
  stepper,
  taskTokens,
  tok,
  windowName,
  windowText,
} from '../hooks/register'

const RAN = (exitCode: number, stdout: string) => ({
  value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
// What the engine answers beneath the mod in every test: no JOB_SEARCH_APP, a fixed clock,
// the workspace root with its status script, and a pane and status line that accept anything.
// `usage` is what `$.session.usage()` answers (the engine's own figures).
const NO_USAGE = () => ({ startedAt: 0, context: { window: 200_000 }, rateLimits: [] as { kind: string; percentUsed: number }[] })
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function workspace(on: any, usage: () => object = NO_USAGE) {
  on('env.get', () => ({ value: null }))
  on('clock.now', () => ({ value: 1_000_000 }))
  on('session.root', () => ({ value: '/workspace' }))
  on('fs.read', () => ({ value: '# script' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.usage', () => ({ value: usage() }))
  on('agent.list', () => ({ value: [] }))
}

const PANE_PROPS = {
  title: 'Job search',
  isFocused: false,
  bodyColumns: 80,
  placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 40 },
  view: {},
}
const LINE = JSON.stringify({
  backend: true,
  frontend: false,
  searches: 1,
  tasks: [
    {
      title: 'Writing tailored CV',
      step: 'Plan',
      done: 1,
      total: 4,
      completed: ['Read CV'],
      waiting_on: null,
      calls: 3,
      input_tokens: 9000,
      output_tokens: 800,
    },
  ],
  gate: { ok: false, kind: 'edit', at: 1, checks: [{ name: 'ruff', ok: true }, { name: 'mypy', ok: false }] },
  git_dirty: 3,
})

describe('Master CV check before cv_writer', () => {
  test('tells the model to stop when the CV does not validate', async ($, on) => {
    workspace(on)
    on('process.run', () => RAN(1, 'data/master_cv.json does not validate:\nbasics.name: Field required'))
    on('skill.prompt', (_$, e) => ({ text: e.text }))

    const out = await $.skill.prompt({ skill: 'cv_writer', text: 'Tailor the CV for Acme' })

    expect(out.text).toMatch(/^job-search-live checked data\/master_cv\.json before this skill: it does not validate\. Stop\./)
    expect(out.text).toMatch(/basics\.name: Field required/)
    expect(out.text).toMatch(/Tailor the CV for Acme$/)
  })

  test('says it cannot tell on any other failure', async ($, on) => {
    workspace(on)
    on('process.run', () => RAN(2, 'Could not run the validation'))
    on('skill.prompt', (_$, e) => ({ text: e.text }))

    const out = await $.skill.prompt({ skill: 'cv_writer', text: 'Tailor' })

    expect(out.text).toMatch(/could not tell whether data\/master_cv\.json validates \(exit 2\)/)
  })

  test('leaves the skill alone when the CV validates, and ignores other skills', async ($, on) => {
    let ran: readonly string[] = []
    workspace(on)
    on('process.run', (_$, e) => {
      ran = e.argv
      return RAN(0, 'data/master_cv.json validates.')
    })
    on('skill.prompt', (_$, e) => ({ text: e.text }))

    expect((await $.skill.prompt({ skill: 'cv_writer', text: 'Tailor' })).text).toBe('Tailor')
    expect(ran).toEqual(['python3', '.claude/hooks/gate.py', '--validate-cv'])
    expect((await $.skill.prompt({ skill: 'job_search', text: 'Find jobs' })).text).toBe('Find jobs')
  })
})

describe('inside the app', () => {
  test('JOB_SEARCH_APP=1 turns the mod off', async ($, on) => {
    let checked = false
    on('env.get', () => ({ value: '1' }))
    on('session.root', () => ({ value: '/workspace' }))
    on('fs.read', () => ({ value: '# script' }))
    on('process.run', () => {
      checked = true
      return RAN(1, 'does not validate')
    })
    on('skill.prompt', (_$, e) => ({ text: e.text }))

    expect((await $.skill.prompt({ skill: 'cv_writer', text: 'Tailor' })).text).toBe('Tailor')
    expect(checked).toBe(false)
  })
})

describe('commands', () => {
  test('/gate reports a failed gate with its tail', async ($, on) => {
    workspace(on)
    on('process.run', (_$, e) =>
      e.argv.includes('--all')
        ? { value: { exitCode: 2, stdout: '', stderr: 'mypy failed:\nsrc/x.py:1: error', isStdoutTruncated: false, isStderrTruncated: false } }
        : RAN(0, LINE),
    )

    const said = await $.command.run({
      command: 'gate',
      args: '',
      origin: { kind: 'composer' },
      presentation: { isFullscreen: false, columns: 80 },
    })

    expect(said.text).toMatch(/^Quality gate failed: ✓ ruff · ✗ mypy/)
    expect(said.text).toMatch(/src\/x\.py:1: error/)
  })
})

describe('pane', () => {
  test('draws on every surface before anything has run', async ($, on) => {
    workspace(on)
    for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
      const ui = await $.ui.mount({ plugin: 'job-search-live', surface, component: 'Pane', requestId: 'job-search-live', props: PANE_PROPS })
      expect(await ui.find({ text: /Waiting for scripts\/live_status\.py/ })).toBeDefined()
      expect(await ui.find({ text: /No subagents yet/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('shows the app task stepper and the gate after /live polls', async ($, on) => {
    workspace(on)
    on('process.run', () => RAN(0, LINE))
    await $.command.run({ command: 'live', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })

    const ui = await $.ui.mount({ plugin: 'job-search-live', surface: 'terminal', component: 'Pane', requestId: 'job-search-live', props: PANE_PROPS })
    expect(await ui.find({ text: /Writing tailored CV/ })).toBeDefined()
    expect(await ui.find({ text: /● Read CV ─ .+ Plan ─ ○ ─ ○/ })).toBeDefined()
    expect(await ui.find({ text: /✗ mypy/ })).toBeDefined()
    expect(await ui.find({ text: /1 search running/ })).toBeDefined()
    expect(await ui.find({ text: /3 calls · 9\.8k tok/ })).toBeDefined()
    await ui.unmount()
  })

  test('counts each model request of the session live, main and subagent', async ($, on) => {
    workspace(on)
    on('process.run', () => RAN(0, LINE))
    on('turn.step', async function* (_$, e) {
      return {
        turnId: e.turnId,
        index: e.index,
        answer: '',
        toolUses: [],
        stopReason: 'end_turn' as const,
        usage: { model: 'claude', input_tokens: 200, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0, output_tokens: 50 },
      }
    })
    for (const agentId of [undefined, 'sub-1']) {
      const stream = $.turn.step({ turnId: 't1', index: 0, model: 'claude', messageCount: 1, agentId })
      for await (const _ of stream) {
        // drain
      }
      await stream.result
    }

    const ui = await $.ui.mount({ plugin: 'job-search-live', surface: 'terminal', component: 'Pane', requestId: 'job-search-live', props: PANE_PROPS })
    expect(await ui.find({ text: /tokens in 2\.4k \(cache 2\.0k\) · out 100 · 2 requests · subagents 1\.3k/ })).toBeDefined()
    await ui.unmount()
  })

  test('reads the session every tick, so the windows move mid-turn without a measure', async ($, on) => {
    const pushes: string[] = []
    let used = 10
    workspace(on, () => ({
      startedAt: 0,
      context: { window: 200_000, tokens: used * 1000, percent: used },
      rateLimits: [{ kind: 'five_hour', percentUsed: used }],
    }))
    on('process.run', (_$, e) => {
      if (e.argv.includes('claude-limits')) {
        pushes.push(e.argv[3] ?? '')
        return RAN(0, '{"ok": true, "recorded": 1}')
      }
      return RAN(0, LINE)
    })
    const live = () =>
      $.command.run({ command: 'live', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    await live()
    used = 55
    await live()
    await live() // unchanged: not sent again

    const ui = await $.ui.mount({ plugin: 'job-search-live', surface: 'terminal', component: 'Pane', requestId: 'job-search-live', props: PANE_PROPS })
    expect(await ui.find({ text: /55% used · 45% left/ })).toBeDefined()
    expect(await ui.find({ text: /context 55% \(55k of 200k\)/ })).toBeDefined()
    await ui.unmount()
    expect(pushes.map(b => JSON.parse(b)[0].percent_used)).toEqual([10, 55])
  })

  test('shows the Claude windows the session measures and passes them to the app', async ($, on) => {
    const runs: string[][] = []
    const measured = {
      context: { window: 200_000, tokens: 50_000, percent: 25 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 42 }, { kind: 'seven_day', percentUsed: 7.5 }],
    }
    workspace(on, () => ({ startedAt: 0, ...measured }))
    on('process.run', (_$, e) => {
      runs.push([...e.argv])
      return RAN(0, LINE)
    })
    on('session.measure', (_$, e) => ({ changed: [...e.changed] }))
    await $.session.measure({ ...measured, changed: ['rateLimits', 'context'] })
    await $.command.run({ command: 'live', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })

    const ui = await $.ui.mount({ plugin: 'job-search-live', surface: 'terminal', component: 'Pane', requestId: 'job-search-live', props: PANE_PROPS })
    expect(await ui.find({ text: /Claude/ })).toBeDefined()
    expect(await ui.find({ text: /42% used · 58% left/ })).toBeDefined()
    expect(await ui.find({ text: /context 25% \(50k of 200k\)/ })).toBeDefined()
    await ui.unmount()
    const pushed = runs.find(a => a.includes('claude-limits'))
    expect(JSON.parse(pushed?.[3] ?? '[]')).toEqual([
      { kind: 'five_hour', percent_used: 42, resets_at: null },
      { kind: 'seven_day', percent_used: 7.5, resets_at: null },
    ])
  })
})

describe('helpers', () => {
  test('parse the status line and draw its parts', () => {
    const s = parseStatus(`noise\n${LINE}\n`)
    expect(s?.git_dirty).toBe(3)
    expect(parseStatus('not json')).toBeNull()
    expect(stepper(s!.tasks[0]!, '◐')).toBe('● Read CV ─ ◐ Plan ─ ○ ─ ○')
    expect(gateLine(null)).toBe('gate not run yet')
    const codex = { label: 'Codex', windows: [{ name: '5h', used_percent: 40.4, resets_at: null, tokens: 5400 }], exhausted_until: null, tokens_today: 8100, calls_today: 3 }
    expect(planLine(codex)).toBe('Codex · today 8.1k tok in 3 calls')
    expect(planLine({ label: 'Codex', windows: [], exhausted_until: null })).toBe('Codex within limits')
    expect(windowText(40.4, null, 5400)).toBe('40% used · 60% left · app 5.4k tok')
    expect(windowText(null, null)).toBe('within limit')
    expect(bar(40)).toBe('████░░░░░░')
    expect([tok(850), tok(12_345), tok(1_200_000)]).toEqual(['850', '12k', '1.2M'])
    expect([windowName('five_hour'), windowName('seven_day_opus'), windowName('5h')]).toEqual(['5h', '7d opus', '5h'])
    expect(taskTokens(s!.tasks[0]!)).toBe('3 calls · 9.8k tok')
    expect(taskTokens({ ...s!.tasks[0]!, calls: 0 })).toBe('')
    expect(readLimits([{ kind: 'five_hour', percentUsed: 9, resetsAt: '2030-01-01T00:00:00Z' }])).toEqual([
      { kind: 'five_hour', percentUsed: 9, resetsAt: 1893456000 },
    ])
    const app = { id: 'claude_code', label: 'Claude Code', windows: [{ name: '5h', used_percent: null, resets_at: null, tokens: 2000 }], exhausted_until: null }
    expect(claudeWindows([{ kind: 'five_hour', percentUsed: 10, resetsAt: null }], app)).toEqual([
      { name: '5h', used: 10, text: '10% used · 90% left · app 2.0k tok' },
    ])
    expect(sessionLine({ input: 0, cacheRead: 0, cacheWrite: 0, output: 0, requests: 0, subagents: 0, contextPercent: null, contextTokens: null, contextWindow: null })).toBe('no model requests yet')
    expect(kindOf('Explore', 'find the search code')).toBe('explorer')
    expect(kindOf('', 'x')).toBe('agent')
  })
})
