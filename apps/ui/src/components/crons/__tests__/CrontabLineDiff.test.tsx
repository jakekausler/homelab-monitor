import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { CrontabLineDiff } from '@/components/crons/CrontabLineDiff'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CrontabLineDiff', () => {
  const SOURCE_PATH = '/etc/crontab'
  const OLD_LINE = '0 4 * * * root /usr/local/bin/cron-with-heartbeat.sh -- /opt/backup.sh'
  const NEW_LINE = '0 4 * * * root /opt/backup.sh'

  it('renders the file path label', () => {
    render(<CrontabLineDiff sourcePath={SOURCE_PATH} oldLine={OLD_LINE} newLine={NEW_LINE} />)
    expect(screen.getByText(`File: ${SOURCE_PATH}`)).toBeInTheDocument()
  })

  it('renders old_line with a minus prefix', () => {
    render(<CrontabLineDiff sourcePath={SOURCE_PATH} oldLine={OLD_LINE} newLine={NEW_LINE} />)
    // The minus sign for the old line
    const minusEl = screen.getAllByText('-')
    expect(minusEl.length).toBeGreaterThan(0)
    // Old line text is rendered
    expect(screen.getByText(OLD_LINE)).toBeInTheDocument()
  })

  it('renders new_line with a plus prefix', () => {
    render(<CrontabLineDiff sourcePath={SOURCE_PATH} oldLine={OLD_LINE} newLine={NEW_LINE} />)
    // The plus sign for the new line
    const plusEl = screen.getAllByText('+')
    expect(plusEl.length).toBeGreaterThan(0)
    // New line text is rendered
    expect(screen.getByText(NEW_LINE)).toBeInTheDocument()
  })

  it('renders heading "Crontab diff"', () => {
    render(<CrontabLineDiff sourcePath={SOURCE_PATH} oldLine={OLD_LINE} newLine={NEW_LINE} />)
    expect(screen.getByText('Crontab diff')).toBeInTheDocument()
  })

  it('shows old_line with strikethrough styling', () => {
    render(<CrontabLineDiff sourcePath={SOURCE_PATH} oldLine={OLD_LINE} newLine={NEW_LINE} />)
    const oldLineEl = screen.getByText(OLD_LINE)
    // The code element wrapping old_line has line-through class
    expect(oldLineEl.className).toContain('line-through')
  })

  it('renders different old and new lines correctly', () => {
    const altOld = '*/5 * * * * root /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/task.sh'
    const altNew = '*/5 * * * * root /usr/bin/task.sh'
    render(<CrontabLineDiff sourcePath="crontab:root" oldLine={altOld} newLine={altNew} />)
    expect(screen.getByText(altOld)).toBeInTheDocument()
    expect(screen.getByText(altNew)).toBeInTheDocument()
    expect(screen.getByText('File: crontab:root')).toBeInTheDocument()
  })
})
