import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as React from 'react'

import { TooltipProvider } from '@/components/ui/tooltip'

vi.mock('@/api/logs', async () => {
  const actual = await vi.importActual<typeof import('@/api/logs')>('@/api/logs')
  return { ...actual, useLogsQuery: vi.fn() }
})

import { useLogsQuery } from '@/api/logs'
import { UnifiThreatsTab } from './UnifiThreatsTab'

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
  return createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
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

describe('UnifiThreatsTab', () => {
  it('pins the query to the audit+firewall security events expr with EMPTY services', async () => {
    renderNode(<UnifiThreatsTab />)
    await screen.findByTestId('unifi-threats-banner')
    const last = vi.mocked(useLogsQuery).mock.calls.at(-1)!
    expect(last[0]).toBe('source_type:udm (service:udm-audit OR service:udm-firewall)')
    expect(last[3]).toBe('')
  })

  it('always shows the honest banner, even with zero rows', async () => {
    renderNode(<UnifiThreatsTab />)
    expect(await screen.findByTestId('unifi-threats-banner')).toBeInTheDocument()
    expect(screen.getByTestId('unifi-threats-banner')).toHaveTextContent(/security-relevant/i)
    // and zero rows -> no-lines empty state coexists with the always-on banner
    expect(screen.getByTestId('no-lines')).toBeInTheDocument()
  })

  it('keeps the banner visible even when lines exist', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(
      makeQueryResult({
        data: {
          pages: [
            {
              lines: [
                {
                  timestamp: '2026-06-21T00:00:00Z',
                  message: 'IPS alert',
                  service: 'udm-audit',
                  severity: 'warning',
                  host: 'udm',
                  stream: 's',
                  fields: {},
                },
              ],
              next_cursor: null,
              has_more: false,
            },
          ],
          pageParams: [],
        },
      }) as never,
    )
    renderNode(<UnifiThreatsTab />)
    expect(await screen.findByTestId('unifi-threats-banner')).toBeInTheDocument()
  })
})
