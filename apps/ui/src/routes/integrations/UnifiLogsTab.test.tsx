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

vi.mock('@/api/logs', async () => {
  const actual = await vi.importActual<typeof import('@/api/logs')>('@/api/logs')
  return { ...actual, useLogsQuery: vi.fn() }
})

import { useLogsQuery } from '@/api/logs'
import { UnifiLogsTab } from './UnifiLogsTab'

function makeQueryResult(over: Partial<Record<string, unknown>> = {}) {
  return {
    data: { pages: [{ lines: [], next_cursor: null, has_more: false }], pageParams: [] },
    error: null,
    isLoading: false,
    isFetching: false,
    isError: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
    ...over,
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
    validateSearch: (s: Record<string, unknown>) => ({
      logsql: typeof s.logsql === 'string' ? s.logsql : undefined,
      services: typeof s.services === 'string' ? s.services : undefined,
      since: typeof s.since === 'string' ? s.since : undefined,
      start: typeof s.start === 'string' ? s.start : undefined,
      end: typeof s.end === 'string' ? s.end : undefined,
    }),
  })
  return createRouter({
    routeTree: rootRoute.addChildren([indexRoute, logsRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
}

function renderNode(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = withRouter(node)
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  localStorage.removeItem('homelab-monitor:timezone')
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult() as never)
})

describe('UnifiLogsTab', () => {
  it('defaults to "all" category and scopes the query to the udm-* wildcard with EMPTY services', async () => {
    renderNode(<UnifiLogsTab />)
    await screen.findByTestId('unifi-logs-refresh')
    const calls = vi.mocked(useLogsQuery).mock.calls
    const last = calls[calls.length - 1]!
    expect(last[0]).toBe('source_type:udm service:udm-*')
    expect(last[3]).toBe('') // services CSV must be empty (wildcard lives in expr)
  })

  it('switching the Firewall chip rebuilds the expr', async () => {
    renderNode(<UnifiLogsTab />)
    fireEvent.click(await screen.findByTestId('unifi-logs-cat-firewall'))
    const calls = vi.mocked(useLogsQuery).mock.calls
    const last = calls[calls.length - 1]!
    expect(last[0]).toBe('source_type:udm service:udm-firewall')
  })

  it('committing a client IP appends a src/dst OR group', async () => {
    renderNode(<UnifiLogsTab />)
    const input = await screen.findByTestId('unifi-logs-ip-input')
    fireEvent.change(input, { target: { value: '10.0.0.5' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const calls = vi.mocked(useLogsQuery).mock.calls
    const last = calls[calls.length - 1]!
    expect(last[0]).toBe('source_type:udm service:udm-* (src:"10.0.0.5" OR dst:"10.0.0.5")')
  })

  it('shows the honest empty-state copy when zero lines', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(makeQueryResult() as never) // 0 lines
    renderNode(<UnifiLogsTab />)
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
  })

  it('renders the unavailable banner on a 502', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(
      makeQueryResult({
        error: new ApiError({
          status: 502,
          code: 'bad_gateway',
          message: 'bad gateway',
          retryAfterSeconds: null,
          details: null,
        }),
      }) as never,
    )
    renderNode(<UnifiLogsTab />)
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })

  it('handles a generic API error (non-502) shows the unavailable state', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(
      makeQueryResult({
        error: new ApiError({
          status: 500,
          code: 'internal_server_error',
          message: 'internal server error',
          retryAfterSeconds: null,
          details: null,
        }),
      }) as never,
    )
    renderNode(<UnifiLogsTab />)
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })
})
