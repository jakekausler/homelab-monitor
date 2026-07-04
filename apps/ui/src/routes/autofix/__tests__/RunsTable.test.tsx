import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { RunsTable } from '../RunsTable'
import type { Run } from '@/api/autofix-runs'

const navigateMock = vi.fn()

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => navigateMock,
}))

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: 'run-1',
    alert_id: null,
    duration_ms: 1500,
    ended_at: '2026-07-03T00:01:00Z',
    exit_code: 0,
    initiated_by: 'operator',
    killed_at: null,
    mode: 'dry_run',
    outcome: 'success',
    runbook_id: 'runbook-1',
    runbook_path: 'runbooks/safe-example',
    started_at: '2026-07-02T23:59:00Z',
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  navigateMock.mockClear()
})

describe('RunsTable', () => {
  it('renders one row per run plus the header row', () => {
    const runs = [makeRun({ id: 'run-1' }), makeRun({ id: 'run-2' })]
    const { container } = render(<RunsTable runs={runs} />)
    // Data rows carry role="button" (overriding the implicit <tr> row role), so
    // count <tr> elements directly: 1 header row + 2 data rows.
    const rows = container.querySelectorAll('tr')
    expect(rows).toHaveLength(3)
    expect(screen.getByTestId('runs-row-run-1')).toBeInTheDocument()
    expect(screen.getByTestId('runs-row-run-2')).toBeInTheDocument()
  })

  it('shows Dry-run mode badge for dry_run mode', () => {
    render(<RunsTable runs={[makeRun({ mode: 'dry_run' })]} />)
    expect(screen.getByTestId('mode-badge-dry_run')).toHaveTextContent('Dry-run')
  })

  it('shows Real mode badge for real mode', () => {
    render(<RunsTable runs={[makeRun({ mode: 'real' })]} />)
    expect(screen.getByTestId('mode-badge-real')).toHaveTextContent('Real')
  })

  it('shows Success outcome badge for success outcome', () => {
    render(<RunsTable runs={[makeRun({ outcome: 'success' })]} />)
    expect(screen.getByTestId('outcome-badge-success')).toHaveTextContent('Success')
  })

  it('shows Failure outcome badge for failure outcome', () => {
    render(<RunsTable runs={[makeRun({ outcome: 'failure' })]} />)
    expect(screen.getByTestId('outcome-badge-failure')).toHaveTextContent('Failure')
  })

  it('shows Killed outcome badge for killed outcome', () => {
    render(<RunsTable runs={[makeRun({ outcome: 'killed' })]} />)
    expect(screen.getByTestId('outcome-badge-killed')).toHaveTextContent('Killed')
  })

  it('shows In flight outcome badge with animate-pulse for in_flight outcome', () => {
    render(<RunsTable runs={[makeRun({ outcome: 'in_flight' })]} />)
    const badge = screen.getByTestId('outcome-badge-in_flight')
    expect(badge).toHaveTextContent('In flight')
    expect(badge.className).toContain('animate-pulse')
  })

  it('clicking a row navigates to the run detail route with the run id', () => {
    render(<RunsTable runs={[makeRun({ id: 'run-abc' })]} />)
    fireEvent.click(screen.getByTestId('runs-row-run-abc'))
    expect(navigateMock).toHaveBeenCalledWith({
      to: '/autofix/history/$run_id',
      params: { run_id: 'run-abc' },
    })
  })

  it('Enter keydown on a row navigates to the run detail route', () => {
    render(<RunsTable runs={[makeRun({ id: 'run-abc' })]} />)
    fireEvent.keyDown(screen.getByTestId('runs-row-run-abc'), { key: 'Enter' })
    expect(navigateMock).toHaveBeenCalledWith({
      to: '/autofix/history/$run_id',
      params: { run_id: 'run-abc' },
    })
  })

  it('Space keydown on a row navigates to the run detail route', () => {
    render(<RunsTable runs={[makeRun({ id: 'run-abc' })]} />)
    fireEvent.keyDown(screen.getByTestId('runs-row-run-abc'), { key: ' ' })
    expect(navigateMock).toHaveBeenCalledWith({
      to: '/autofix/history/$run_id',
      params: { run_id: 'run-abc' },
    })
  })

  it('other keydowns do not navigate', () => {
    render(<RunsTable runs={[makeRun({ id: 'run-abc' })]} />)
    fireEvent.keyDown(screen.getByTestId('runs-row-run-abc'), { key: 'Tab' })
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('operator-initiated row shows "operator" text and no alert_id', () => {
    render(
      <RunsTable runs={[makeRun({ id: 'run-op', initiated_by: 'operator', alert_id: null })]} />,
    )
    expect(screen.getByText('operator')).toBeInTheDocument()
    expect(screen.queryByTestId('runs-row-alert-run-op')).not.toBeInTheDocument()
  })

  it('alert-initiated row with alert_id shows an 8-char truncated id', () => {
    render(
      <RunsTable
        runs={[
          makeRun({
            id: 'run-alert',
            initiated_by: 'alert',
            alert_id: 'abcdefgh12345',
          }),
        ]}
      />,
    )
    expect(screen.getByTestId('runs-row-alert-run-alert')).toHaveTextContent('abcdefgh')
  })

  it('renders em-dash when duration_ms is null', () => {
    render(<RunsTable runs={[makeRun({ id: 'run-null-dur', duration_ms: null })]} />)
    expect(screen.getByTestId('runs-row-run-null-dur')).toHaveTextContent('—')
  })

  it('formats a small duration_ms via formatDuration(seconds)', () => {
    // duration_ms=1500 -> 1.5s -> formatDuration divides by 1000 -> 1.500s (< 10s branch, 3 decimals)
    render(<RunsTable runs={[makeRun({ id: 'run-small-dur', duration_ms: 1500 })]} />)
    expect(screen.getByTestId('runs-row-run-small-dur')).toHaveTextContent('1.500s')
  })

  it('formats a large duration_ms (hours) via formatDuration(seconds)', () => {
    // duration_ms = 2 * 3600 * 1000 + 5*60*1000 = 7,500,000ms -> 7500s -> 2h 5m
    render(<RunsTable runs={[makeRun({ id: 'run-large-dur', duration_ms: 7_500_000 })]} />)
    expect(screen.getByTestId('runs-row-run-large-dur')).toHaveTextContent('2h 5m')
  })

  it('has no delete affordances anywhere', () => {
    const { container } = render(<RunsTable runs={[makeRun({ id: 'run-1' })]} />)
    expect(screen.queryAllByText(/^delete$/i).length).toBe(0)
    expect(container.querySelectorAll('[data-testid*="delete" i]').length).toBe(0)
  })
})
