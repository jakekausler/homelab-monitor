import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseQueryResult } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import React from 'react'

import { RunsHistoryPage } from '../RunsHistoryPage'
import { useRunsList } from '@/api/autofix-runs'
import type { Run } from '@/api/autofix-runs'
import type { ApiError } from '@/api/client'

vi.mock('@/api/autofix-runs', async () => {
  const actual = await vi.importActual<typeof import('@/api/autofix-runs')>('@/api/autofix-runs')
  return {
    ...actual,
    useRunsList: vi.fn(),
  }
})

vi.mock('@/api/runbooks', async () => {
  const actual = await vi.importActual<typeof import('@/api/runbooks')>('@/api/runbooks')
  return {
    ...actual,
    useRunbooks: vi.fn().mockReturnValue({ data: { items: [] }, isLoading: false, isError: false }),
  }
})

const navigateMock = vi.fn()

vi.mock('@tanstack/react-router', () => ({
  useSearch: () => ({
    runbook_id: undefined,
    mode: undefined,
    outcome: undefined,
    initiator: undefined,
    since: undefined,
    until: undefined,
    page: undefined,
  }),
  useNavigate: () => navigateMock,
  Link: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props}>{children}</a>
  ),
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
    started_at: '2026-07-03T00:00:00Z',
    ...overrides,
  }
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

describe('RunsHistoryPage', () => {
  it('renders heading and description', () => {
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ data: { items: [], total_count: 0, limit: 100, offset: 0 } }),
    )
    render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    expect(screen.getByRole('heading', { level: 1, name: 'Auto-fix history' })).toBeInTheDocument()
    expect(
      screen.getByText('Runs from the auto-fix subsystem — dry-run and real, most recent first.'),
    ).toBeInTheDocument()
  })

  it('renders a table row per run when 3 runs are returned', () => {
    const runs = [makeRun({ id: 'run-1' }), makeRun({ id: 'run-2' }), makeRun({ id: 'run-3' })]
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ data: { items: runs, total_count: 3, limit: 100, offset: 0 } }),
    )
    render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    expect(screen.getByTestId('runs-row-run-1')).toBeInTheDocument()
    expect(screen.getByTestId('runs-row-run-2')).toBeInTheDocument()
    expect(screen.getByTestId('runs-row-run-3')).toBeInTheDocument()
  })

  it('renders empty state text when no runs match filters', () => {
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ data: { items: [], total_count: 0, limit: 100, offset: 0 } }),
    )
    render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    expect(screen.getByText('No runs match these filters.')).toBeInTheDocument()
  })

  it('renders alert-role error message on isError', () => {
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ isError: true, error: { message: 'network down' } }),
    )
    render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load runs: network down')
  })

  it('renders pagination summary for a multi-page total_count', () => {
    const runs = [makeRun({ id: 'run-1' })]
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ data: { items: runs, total_count: 250, limit: 100, offset: 0 } }),
    )
    render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    // page defaults to 1; total_count=250, RUNS_PAGE_SIZE=100 -> 3 pages
    expect(screen.getByTestId('pagination-summary')).toHaveTextContent('Page 1 of 3 · 250 runs')
  })

  it('has no delete affordances', () => {
    const runs = [makeRun({ id: 'run-1' })]
    vi.mocked(useRunsList).mockReturnValue(
      mockQuery({ data: { items: runs, total_count: 1, limit: 100, offset: 0 } }),
    )
    const { container } = render(<RunsHistoryPage />, { wrapper: makeWrapper() })
    expect(screen.queryAllByText(/^delete$/i).length).toBe(0)
    expect(container.querySelectorAll('[data-testid*="delete" i]').length).toBe(0)
    expect(container.querySelectorAll('[data-testid*="edit" i]').length).toBe(0)
  })
})
