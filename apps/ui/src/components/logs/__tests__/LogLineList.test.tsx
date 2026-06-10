import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LogLineList, getColumnValue } from '@/components/logs/LogLineList'
import { severityTintClass } from '@/components/logs/severity'
import type { LogLine } from '@/components/logs/types'

afterEach(cleanup)

function line(
  message: string,
  severity: string | null = null,
  fields: Record<string, unknown> = {},
): LogLine {
  return {
    timestamp: '2026-05-21T14:30:00Z',
    message,
    stream: 'stdout',
    severity,
    host: null,
    service: null,
    fields,
  }
}

describe('LogLineList', () => {
  it('renders lines with local timestamps by default', () => {
    render(<LogLineList lines={[line('a'), line('b')]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.textContent).toContain('a')
    expect(body.textContent).toContain('b')
    // 2026-05-21T14:30:00Z in America/New_York (EDT, UTC-4) = 10:30:00.
    expect(body.textContent).toContain('2026-05-21 10:30:00 EDT')
  })

  it('renders UTC timestamps when timezone="utc"', () => {
    render(<LogLineList lines={[line('a')]} testId="x" timezone="utc" />)

    const body = screen.getByTestId('x')
    expect(body.textContent).toContain('2026-05-21 14:30:00 UTC')
  })

  it('sets a title tooltip showing the other zone', () => {
    render(<LogLineList lines={[line('a')]} testId="x" timezone="local" />)

    // The timestamp span carries the UTC format as its title.
    const tsSpan = screen.getByTitle('2026-05-21 14:30:00 UTC')
    expect(tsSpan).toBeInTheDocument()
    expect(tsSpan.textContent).toBe('2026-05-21 10:30:00 EDT')
  })

  it('renders empty with emptyContent', () => {
    render(<LogLineList lines={[]} testId="x" emptyContent={<span>nope</span>} />)

    expect(screen.getByTestId('x')).toBeInTheDocument()
    expect(screen.getByText('nope')).toBeInTheDocument()
  })

  it('renders empty without emptyContent', () => {
    render(<LogLineList lines={[]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body).toBeInTheDocument()
    expect(body.textContent).toBe('')
  })

  it('applies severity tint red for error', () => {
    render(<LogLineList lines={[line('e', 'error')]} testId="x" />)

    const body = screen.getByTestId('x')
    const redDiv = body.querySelector('.text-red-500')
    expect(redDiv).toBeInTheDocument()
    expect(redDiv).toHaveTextContent('e')
  })

  it('applies severity tint yellow for warn', () => {
    render(<LogLineList lines={[line('w', 'warn')]} testId="x" />)

    const body = screen.getByTestId('x')
    const yellowDiv = body.querySelector('.text-yellow-500')
    expect(yellowDiv).toBeInTheDocument()
    expect(yellowDiv).toHaveTextContent('w')
  })

  it('does not tint for info severity', () => {
    render(<LogLineList lines={[line('i', 'info')]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.querySelector('.text-red-500')).not.toBeInTheDocument()
    expect(body.querySelector('.text-yellow-500')).not.toBeInTheDocument()
  })

  it('does not tint for null severity', () => {
    render(<LogLineList lines={[line('n', null)]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.querySelector('.text-red-500')).not.toBeInTheDocument()
    expect(body.querySelector('.text-yellow-500')).not.toBeInTheDocument()
  })

  it('Enter key activates a clickable row (calls onLineClick)', async () => {
    const onLineClick = vi.fn()
    render(<LogLineList lines={[line('key-test')]} testId="x" onLineClick={onLineClick} />)
    const row = screen.getByRole('button')
    row.focus()
    await userEvent.keyboard('{Enter}')
    expect(onLineClick).toHaveBeenCalledTimes(1)
    expect(onLineClick.mock.calls[0]?.[0]).toMatchObject({ message: 'key-test' })
  })

  it('Space key activates a clickable row (calls onLineClick)', async () => {
    const onLineClick = vi.fn()
    render(<LogLineList lines={[line('space-test')]} testId="x" onLineClick={onLineClick} />)
    const row = screen.getByRole('button')
    row.focus()
    await userEvent.keyboard(' ')
    expect(onLineClick).toHaveBeenCalledTimes(1)
  })

  // STAGE-004-018B — back-compat guard: NO columns prop → no multi-column path.
  it('renders the 2-column path (no log-row-columns) when no columns prop', () => {
    render(<LogLineList lines={[line('x')]} testId="x" />)
    const body = screen.getByTestId('x')
    expect(screen.queryByTestId('log-row-columns')).toBeNull()
    // original ts + message still render
    expect(body.textContent).toContain('x')
    expect(body.textContent).toContain('2026-05-21 10:30:00 EDT')
  })

  it('renders the 2-column path when columns is an empty array', () => {
    render(<LogLineList lines={[line('x')]} testId="x" columns={[]} />)
    expect(screen.queryByTestId('log-row-columns')).toBeNull()
  })

  // STAGE-004-018B — multi-column render.
  it('renders one cell per configured column with values', () => {
    render(
      <LogLineList
        lines={[
          {
            ...line('msg', 'error', { region: 'us' }),
            host: 'nas01',
          },
        ]}
        testId="x"
        columns={['host', 'severity']}
      />,
    )
    expect(screen.getByTestId('log-row-columns')).toBeInTheDocument()
    const cells = screen.getAllByTestId('log-cell')
    expect(cells).toHaveLength(2)
    const hostCell = cells.find((c) => c.getAttribute('data-field') === 'host')
    const sevCell = cells.find((c) => c.getAttribute('data-field') === 'severity')
    expect(hostCell?.textContent).toBe('nas01')
    expect(sevCell?.textContent).toBe('error')
    // severity column carries the red tint class
    expect(sevCell?.className).toContain('text-red-500')
  })

  it('renders an empty cell for a missing column field (no crash)', () => {
    render(<LogLineList lines={[line('msg')]} testId="x" columns={['nope']} />)
    const cells = screen.getAllByTestId('log-cell')
    expect(cells).toHaveLength(1)
    expect(cells[0]?.textContent).toBe('')
  })

  it('sets inline gridTemplateColumns = auto + N + 1fr', () => {
    render(<LogLineList lines={[line('msg')]} testId="x" columns={['host', 'severity']} />)
    const row = screen.getByTestId('log-row-columns')
    // 'auto auto auto 1fr' (timestamp + 2 columns + message)
    expect(row.style.gridTemplateColumns).toBe('auto auto auto 1fr')
  })

  describe('severityTintClass', () => {
    it('returns text-red-500 for error', () => {
      expect(severityTintClass('error')).toBe('text-red-500')
    })

    it('returns text-red-500 for critical', () => {
      expect(severityTintClass('critical')).toBe('text-red-500')
    })

    it('returns text-red-500 for alert', () => {
      expect(severityTintClass('alert')).toBe('text-red-500')
    })

    it('returns text-red-500 for emergency', () => {
      expect(severityTintClass('emergency')).toBe('text-red-500')
    })

    it('returns text-yellow-500 for warn', () => {
      expect(severityTintClass('warn')).toBe('text-yellow-500')
    })

    it('returns empty string for info', () => {
      expect(severityTintClass('info')).toBe('')
    })

    it('returns empty string for null', () => {
      expect(severityTintClass(null)).toBe('')
    })

    it('returns empty string for undefined', () => {
      expect(severityTintClass(undefined)).toBe('')
    })
  })

  describe('getColumnValue', () => {
    it('resolves promoted severity/host/service first', () => {
      const l = { ...line('m', 'warn'), host: 'h1', service: 'svc' }
      expect(getColumnValue(l, 'severity')).toBe('warn')
      expect(getColumnValue(l, 'host')).toBe('h1')
      expect(getColumnValue(l, 'service')).toBe('svc')
    })

    it('resolves from the fields bag', () => {
      const l = line('m', null, { region: 'us-east' })
      expect(getColumnValue(l, 'region')).toBe('us-east')
    })

    it('returns empty string for a missing field', () => {
      expect(getColumnValue(line('m'), 'absent')).toBe('')
    })

    it('returns empty string for null promoted fields', () => {
      expect(getColumnValue(line('m'), 'host')).toBe('')
    })

    it('coerces a numeric field value to string', () => {
      const l = line('m', null, { count: 42 })
      expect(getColumnValue(l, 'count')).toBe('42')
    })

    it('coerces a boolean field value to string', () => {
      const l = line('m', null, { ok: true })
      expect(getColumnValue(l, 'ok')).toBe('true')
    })

    it('serializes object/array field values as compact JSON', () => {
      const l = line('msg', null, { meta: { a: 1 }, tags: ['x', 'y'] })
      expect(getColumnValue(l, 'meta')).toBe('{"a":1}')
      expect(getColumnValue(l, 'tags')).toBe('["x","y"]')
    })
  })
})
