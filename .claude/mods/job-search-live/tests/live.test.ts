import { describe, expect, test } from 'claude-code/testing'

import { gateLine, kindOf, parseStatus, planLine, stepper } from '../hooks/register'

const RAN = (exitCode: number, stdout: string) => ({
  value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
// What the engine answers beneath the mod in every test: no JOB_SEARCH_APP, a fixed clock,
// the workspace root with its status script, and a pane and status line that accept anything.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function workspace(on: any) {
  on('env.get', () => ({ value: null }))
  on('clock.now', () => ({ value: 1_000_000 }))
  on('session.root', () => ({ value: '/workspace' }))
  on('fs.read', () => ({ value: '# script' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
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
  tasks: [{ title: 'Writing tailored CV', step: 'Plan', done: 1, total: 4, completed: ['Read CV'], waiting_on: null }],
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
    await ui.unmount()
  })
})

describe('helpers', () => {
  test('parse the status line and draw its parts', () => {
    const s = parseStatus(`noise\n${LINE}\n`)
    expect(s?.git_dirty).toBe(3)
    expect(parseStatus('not json')).toBeNull()
    expect(stepper(s!.tasks[0]!, '◐')).toBe('● Read CV ─ ◐ Plan ─ ○ ─ ○')
    expect(gateLine(null)).toBe('gate not run yet')
    expect(planLine({ label: 'Codex', windows: [{ name: '5h', used_percent: 40.4, resets_at: null }], exhausted_until: null })).toBe('Codex 5h 40%')
    expect(kindOf('Explore', 'find the search code')).toBe('explorer')
    expect(kindOf('', 'x')).toBe('agent')
  })
})
