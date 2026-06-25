import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as React from 'react'

import { ApiError } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'

// Mock ONLY the query hook; keep identitiesToServicesCsv REAL so the service
// scope assertion below is genuine. vi.importActual preserves the rest.
vi.mock('@/api/logs', async () => {
  const actual = await vi.importActual<typeof import('@/api/logs')>('@/api/logs')
  return { ...actual, useLogsQuery: vi.fn() }
})

import { useLogsQuery } from '@/api/logs'
import { PiholeLogsTab } from './PiholeLogsTab'

afterEach(() => {
  cleanup()
  localStorage.removeItem('homelab-monitor:timezone')
  vi.clearAllMocks()
})

// Build a LogsQueryResponse page. LogLine = { fields, host, message, service, severity, stream, timestamp }.
function makeLine(
  overrides: Partial<{ message: string; severity: string; timestamp: string }> = {},
) {
  return {
    fields: {},
    host: null,
    message: overrides.message ?? 'WARNING something failed',
    service: 'pihole-unbound',
    severity: overrides.severity ?? 'info', // FTL lines land as info — Vector doesn't parse FTL format
    stream: 'docker:pihole-unbound',
    timestamp: overrides.timestamp ?? '2026-05-21T14:30:00Z',
  }
}

function makeQueryResult(
  overrides: Partial<{
    lines: ReturnType<typeof makeLine>[]
    error: ApiError | null
    isLoading: boolean
    isFetching: boolean
    hasNextPage: boolean
    isFetchingNextPage: boolean
    data: unknown
  }> = {},
) {
  const hasError = overrides.error != null
  const lines = overrides.lines ?? [makeLine()]
  return {
    data:
      'data' in overrides
        ? overrides.data
        : hasError
          ? undefined
          : { pages: [{ has_more: false, lines, next_cursor: null }], pageParams: [undefined] },
    error: overrides.error ?? null,
    isLoading: overrides.isLoading ?? false,
    isFetching: overrides.isFetching ?? false,
    hasNextPage: overrides.hasNextPage ?? false,
    isFetchingNextPage: overrides.isFetchingNextPage ?? false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
  }
}

function withRouter(node: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <>{node}</>,
  })
  const logsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/logs',
    component: () => <div>logs</div>,
    validateSearch: (search: Record<string, unknown>) => ({
      logsql: typeof search.logsql === 'string' ? search.logsql : undefined,
      since: typeof search.since === 'string' ? search.since : undefined,
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
      services: typeof search.services === 'string' ? search.services : undefined,
    }),
  })
  return createRouter({
    routeTree: rootRoute.addChildren([indexRoute, logsRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = withRouter(<PiholeLogsTab />)
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    ),
  }
}

describe('PiholeLogsTab', () => {
  beforeEach(() => {
    vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult() as never)
  })

  it('renders FTL log lines in the available state', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(
      makeQueryResult({ lines: [makeLine({ message: 'WARNING FTL boom' })] }) as never,
    )
    renderTab()
    const body = await screen.findByTestId('logs-body')
    expect(body.textContent).toContain('WARNING FTL boom')
  })

  it('defaults to errors-only and scopes the query to docker:pihole-unbound', async () => {
    renderTab()
    await screen.findByTestId('logs-body')
    const calls = vi.mocked(useLogsQuery).mock.calls
    const lastCall = calls[calls.length - 1]!
    // Text-match expression (NOT severity:) — FTL lines all land as severity:info
    expect(lastCall[0]).toBe('WARNING OR ERROR OR CRITICAL')
    expect(lastCall[3]).toBe('docker:pihole-unbound')
    // The toggle reads "Errors only" while errorsOnly is true.
    expect(screen.getByTestId('pihole-logs-errors-toggle')).toHaveTextContent('Errors only')
  })

  it('toggling "Errors only" swaps expr to match-all *', async () => {
    renderTab()
    await screen.findByTestId('logs-body')
    fireEvent.click(screen.getByTestId('pihole-logs-errors-toggle'))
    const calls = vi.mocked(useLogsQuery).mock.calls
    const lastCall = calls[calls.length - 1]!
    expect(lastCall[0]).toBe('*')
    expect(screen.getByTestId('pihole-logs-errors-toggle')).toHaveTextContent('All lines')
  })

  it('renders the no_lines empty state', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult({ lines: [] }) as never)
    renderTab()
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
  })

  it('renders the unavailable banner on a 502 error', async () => {
    const err = new ApiError({
      status: 502,
      code: 'upstream_unavailable',
      message: 'logs backend unavailable',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult({ error: err }) as never)
    renderTab()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })

  it('renders no lines and no crash on a generic (non-502) error', () => {
    const err = new ApiError({
      status: 500,
      code: 'internal',
      message: 'boom',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult({ error: err }) as never)
    renderTab()
    // No lines body, no unavailable banner — benign empty.
    expect(screen.queryByTestId('logs-body')).toBeNull()
    expect(screen.queryByTestId('unavailable-banner')).toBeNull()
  })

  it('Refresh calls refetch', async () => {
    const refetch = vi.fn()
    vi.mocked(useLogsQuery).mockReturnValue({
      ...(makeQueryResult() as object),
      refetch,
    } as never)
    renderTab()
    fireEvent.click(await screen.findByTestId('pihole-logs-refresh'))
    expect(refetch).toHaveBeenCalled()
  })

  it('renders an Open in Explorer link scoped to the pihole-unbound service', async () => {
    renderTab()
    const el = await screen.findByTestId('open-in-explorer')
    const anchor = el.closest('a')
    expect(anchor).not.toBeNull()
    const href = anchor!.getAttribute('href') ?? ''
    const params = new URLSearchParams(href.split('?')[1])
    expect(params.get('services')).toBe('docker:pihole-unbound')
    // errors-only default → text-match expr carried as logsql.
    expect(params.get('logsql')).toBe('WARNING OR ERROR OR CRITICAL')
    // default 1h preset → since=1h.
    expect(params.get('since')).toBe('1h')
  })
})
