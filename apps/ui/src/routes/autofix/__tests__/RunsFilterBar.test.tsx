import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseQueryResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

import { RunsFilterBar } from '../RunsFilterBar'
import { useRunbooks } from '@/api/runbooks'
import type { Runbook } from '@/api/runbooks'
import type { ApiError } from '@/api/client'
import type { RunsListFilters } from '@/api/autofix-runs'

vi.mock('@/api/runbooks', async () => {
  const actual = await vi.importActual<typeof import('@/api/runbooks')>('@/api/runbooks')
  return {
    ...actual,
    useRunbooks: vi.fn(),
  }
})

const navigateMock = vi.fn<(opts: { to: string; search: Record<string, unknown> }) => void>()

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => navigateMock,
}))

function mockQuery<TData>(
  overrides: Record<string, unknown> = {},
): UseQueryResult<TData, ApiError> {
  return {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    ...overrides,
  } as unknown as UseQueryResult<TData, ApiError>
}

const RUNBOOK: Runbook = {
  id: 'runbook-1',
  path: 'runbooks/safe-example',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [],
  risk_tag: 'safe',
  dry_run_required: false,
  rate_limit_per_hour: null,
  cooldown_seconds: null,
  enabled: true,
  auto_trigger: false,
  content_hash: 'hash',
}

const DEFAULT_FILTERS: RunsListFilters = {
  runbook_id: undefined,
  mode: undefined,
  outcome: undefined,
  initiator: undefined,
  since: '2026-06-03T00:00:00.000Z',
  until: '2026-07-03T00:00:00.000Z',
}

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children)
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  navigateMock.mockClear()
})

describe('RunsFilterBar', () => {
  it('renders all filter controls with defaults', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [RUNBOOK] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    expect(screen.getByTestId('filter-runbook')).toBeInTheDocument()
    expect(screen.getByTestId('filter-mode')).toBeInTheDocument()
    expect(screen.getByTestId('filter-outcome')).toBeInTheDocument()
    expect(screen.getByTestId('filter-initiator')).toBeInTheDocument()
    expect(screen.getByTestId('filter-since')).toBeInTheDocument()
    expect(screen.getByTestId('filter-until')).toBeInTheDocument()
    expect(screen.getByTestId('filter-reset')).toBeInTheDocument()
  })

  it('runbook filter defaults to Any and lists runbook basenames', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [RUNBOOK] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    const select: HTMLSelectElement = screen.getByTestId('filter-runbook')
    expect(select.value).toBe('__any__')
    expect(screen.getByText('safe-example')).toBeInTheDocument()
  })

  it('selecting a mode filter navigates with the patched search and resets page to 1', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    fireEvent.change(screen.getByTestId('filter-mode'), { target: { value: 'real' } })
    expect(navigateMock).toHaveBeenCalledWith({
      to: '/autofix/history',
      search: {
        runbook_id: undefined,
        mode: 'real',
        outcome: undefined,
        initiator: undefined,
        since: DEFAULT_FILTERS.since,
        until: DEFAULT_FILTERS.until,
        page: 1,
      },
    })
  })

  it('selecting a specific outcome filter value navigates with that outcome set', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    fireEvent.change(screen.getByTestId('filter-outcome'), { target: { value: 'failure' } })
    expect(navigateMock).toHaveBeenCalledTimes(1)
    const opts = navigateMock.mock.calls[0]![0] as {
      to: string
      search: Record<string, string | undefined | number>
    }
    expect(opts.search.outcome).toBe('failure')
  })

  it('changing the since date converts to start-of-day ISO', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    fireEvent.change(screen.getByTestId('filter-since'), { target: { value: '2026-06-15' } })
    expect(navigateMock).toHaveBeenCalledTimes(1)
    const opts = navigateMock.mock.calls[0]![0] as {
      to: string
      search: Record<string, string | undefined | number>
    }
    expect(opts.search.since).toBe('2026-06-15T00:00:00.000Z')
  })

  it('changing the until date converts to end-of-day ISO', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    fireEvent.change(screen.getByTestId('filter-until'), { target: { value: '2026-06-15' } })
    expect(navigateMock).toHaveBeenCalledTimes(1)
    const opts = navigateMock.mock.calls[0]![0] as {
      to: string
      search: Record<string, string | undefined | number>
    }
    expect(opts.search.until).toBe('2026-06-15T23:59:59.999Z')
  })

  it('clearing the since date input falls back to the existing filter value (dateInputToIso returns undefined, patch keeps prior since)', () => {
    // dateInputToIso('') returns undefined; patch() only overrides `since` when
    // the partial value is !== undefined, so an undefined partial falls back to
    // the current filters.since rather than clearing it.
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunsFilterBar filters={DEFAULT_FILTERS} />, { wrapper: makeWrapper() })
    fireEvent.change(screen.getByTestId('filter-since'), { target: { value: '' } })
    expect(navigateMock).toHaveBeenCalledTimes(1)
    const opts = navigateMock.mock.calls[0]![0] as {
      to: string
      search: Record<string, string | undefined | number>
    }
    expect(opts.search.since).toBe(DEFAULT_FILTERS.since)
  })

  it('Reset button navigates with all filters cleared and page 1', () => {
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(
      <RunsFilterBar
        filters={{ ...DEFAULT_FILTERS, mode: 'real', outcome: 'success', initiator: 'operator' }}
      />,
      { wrapper: makeWrapper() },
    )
    fireEvent.click(screen.getByTestId('filter-reset'))
    expect(navigateMock).toHaveBeenCalledWith({
      to: '/autofix/history',
      search: {
        runbook_id: undefined,
        mode: undefined,
        outcome: undefined,
        initiator: undefined,
        since: undefined,
        until: undefined,
        page: 1,
      },
    })
  })
})
