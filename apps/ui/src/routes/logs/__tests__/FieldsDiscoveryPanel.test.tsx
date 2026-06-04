import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FieldsDiscoveryPanel } from '../FieldsDiscoveryPanel'
import { useLogsFieldsQuery } from '@/api/logs'
import type { Schema } from '@/api/types'

vi.mock('@/api/logs', () => ({
  useLogsFieldsQuery: vi.fn(),
}))

afterEach(() => {
  cleanup()
})

type LogsFieldsResponse = Schema<'LogsFieldsResponse'>

const SAMPLE: LogsFieldsResponse = {
  fields: [
    { name: 'level', coverage: 1.0, type_hint: 'string', sample_values: ['error', 'warn'] },
    { name: 'json.user_id', coverage: 0.45, type_hint: 'numeric', sample_values: ['42'] },
  ],
  sampled_lines: 2,
  truncated: false,
}

function mockQuery(overrides: Partial<ReturnType<typeof useLogsFieldsQuery>>): void {
  vi.mocked(useLogsFieldsQuery).mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    ...overrides,
  } as ReturnType<typeof useLogsFieldsQuery>)
}

function renderPanel(props?: {
  onAddFieldFilter?: (f: string, v: string) => void
  onSelectField?: (f: string) => void
}) {
  const baseProps = {
    expr: '*',
    start: '2026-05-07T00:00:00Z',
    end: '2026-05-07T01:00:00Z',
    services: '',
    onAddFieldFilter: props?.onAddFieldFilter ?? vi.fn(),
  }
  return render(
    <FieldsDiscoveryPanel
      {...baseProps}
      {...(props?.onSelectField !== undefined && { onSelectField: props.onSelectField })}
    />,
  )
}

describe('FieldsDiscoveryPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders field rows in server order with coverage + type', () => {
    mockQuery({ data: SAMPLE })
    renderPanel()
    const rows = screen.getAllByTestId('fields-discovery-row')
    expect(rows.map((r) => r.getAttribute('data-field'))).toEqual(['level', 'json.user_id'])
    const coverages = screen.getAllByTestId('fields-discovery-coverage').map((e) => e.textContent)
    expect(coverages).toEqual(['100%', '45%'])
  })

  it('clicking a sample chip injects field:value', () => {
    const onAdd = vi.fn()
    mockQuery({ data: SAMPLE })
    renderPanel({ onAddFieldFilter: onAdd })
    const chip = screen
      .getAllByTestId('fields-discovery-chip')
      .find(
        (c) => c.getAttribute('data-field') === 'level' && c.getAttribute('data-value') === 'error',
      )
    fireEvent.click(chip!)
    expect(onAdd).toHaveBeenCalledWith('level', 'error')
  })

  it('name filter narrows the list', () => {
    mockQuery({ data: SAMPLE })
    renderPanel()
    fireEvent.change(screen.getByTestId('fields-discovery-search'), {
      target: { value: 'user' },
    })
    const rows = screen.getAllByTestId('fields-discovery-row')
    expect(rows).toHaveLength(1)
    expect(rows[0]?.getAttribute('data-field')).toBe('json.user_id')
  })

  it('clicking a field name calls onSelectField', () => {
    const onSelect = vi.fn()
    mockQuery({ data: SAMPLE })
    renderPanel({ onSelectField: onSelect })
    const name = screen
      .getAllByTestId('fields-discovery-name')
      .find((n) => n.getAttribute('data-field') === 'level')
    fireEvent.click(name!)
    expect(onSelect).toHaveBeenCalledWith('level')
  })

  it('renders field name as non-interactive span when onSelectField is not provided', () => {
    mockQuery({ data: SAMPLE })
    renderPanel()
    const nameEls = screen.getAllByTestId('fields-discovery-name')
    for (const el of nameEls) {
      expect(el.tagName.toLowerCase()).toBe('span')
      expect(el).not.toHaveAttribute('type')
    }
  })

  it('shows loading state', () => {
    mockQuery({ isLoading: true })
    renderPanel()
    expect(screen.getByTestId('fields-discovery-loading')).toBeInTheDocument()
  })

  it('shows error state', () => {
    mockQuery({ isError: true })
    renderPanel()
    expect(screen.getByTestId('fields-discovery-error')).toBeInTheDocument()
  })

  it('shows empty state', () => {
    mockQuery({ data: { fields: [], sampled_lines: 0, truncated: false } })
    renderPanel()
    expect(screen.getByTestId('fields-discovery-empty')).toBeInTheDocument()
  })
})
