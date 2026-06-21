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
import { UnifiDeviceLogsSection } from './UnifiDeviceLogsSection'

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

describe('UnifiDeviceLogsSection', () => {
  it('renders the gateway-sourced honest note', async () => {
    renderNode(<UnifiDeviceLogsSection />)
    expect(await screen.findByTestId('unifi-device-logs-note')).toHaveTextContent(
      /gateway-sourced/i,
    )
  })

  it('queries the udm-* "all" stream (no per-device IP filter) with EMPTY services', async () => {
    renderNode(<UnifiDeviceLogsSection />)
    await screen.findByTestId('unifi-device-logs-note')
    const last = vi.mocked(useLogsQuery).mock.calls.at(-1)!
    expect(last[0]).toBe('source_type:udm service:udm-*')
    expect(last[3]).toBe('')
  })

  it('shows the honest empty-state copy when zero lines', async () => {
    renderNode(<UnifiDeviceLogsSection />)
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
  })
})
