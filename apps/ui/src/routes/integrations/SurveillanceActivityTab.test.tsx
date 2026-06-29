import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as React from 'react'

import { ApiError } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'

// Mock ONLY the query hook from logs; keep the rest real.
vi.mock('@/api/logs', async () => {
  const actual = await vi.importActual<typeof import('@/api/logs')>('@/api/logs')
  return { ...actual, useLogsQuery: vi.fn() }
})
vi.mock('@/api/surveillance', () => ({
  useSurveillanceCameras: vi.fn(),
}))

import { useLogsQuery } from '@/api/logs'
import { useSurveillanceCameras } from '@/api/surveillance'
import { SurveillanceActivityTab } from './SurveillanceActivityTab'

afterEach(() => {
  cleanup()
  localStorage.removeItem('homelab-monitor:timezone')
  vi.clearAllMocks()
})

function makeLine(overrides: Partial<{ message: string; timestamp: string }> = {}) {
  return {
    fields: {},
    host: null,
    message: overrides.message ?? 'DSM auth login success',
    service: 'synology-auth',
    severity: 'info',
    stream: 'syslog:synology-auth',
    timestamp: overrides.timestamp ?? '2026-05-21T14:30:00Z',
  }
}

function makeLogsResult(
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

// A UseQueryResult-shaped stub for useSurveillanceCameras.
function makeCamerasResult(data: unknown, overrides: Partial<{ error: ApiError }> = {}) {
  return {
    data,
    error: overrides.error ?? null,
    isPending: false,
    isError: overrides.error != null,
    isSuccess: data !== undefined,
  }
}

function happyCamerasData() {
  return {
    cameras: [
      {
        camera: 'FrontDoor',
        connected: true,
        status: 3,
        recordings_count: 42,
        recordings_bytes: 123456789,
        model: 'X',
        ip: '10.0.0.5',
        vendor: 'V',
      },
      {
        camera: 'Garage',
        connected: false,
        status: null,
        recordings_count: null,
        recordings_bytes: null,
        model: null,
        ip: null,
        vendor: null,
      },
      {
        camera: 'ZeroCam',
        connected: true,
        status: 1,
        recordings_count: 0,
        recordings_bytes: 0,
        model: null,
        ip: null,
        vendor: null,
      },
    ],
    events_today: 5,
    events_total_all: 1000,
    recordings_total: 777,
    recordings_bytes_total: 987654321,
    data_available: true,
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

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = withRouter(<SurveillanceActivityTab />)
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('SurveillanceActivityTab', () => {
  beforeEach(() => {
    vi.mocked(useLogsQuery).mockReturnValue(makeLogsResult() as never)
    vi.mocked(useSurveillanceCameras).mockReturnValue(
      makeCamerasResult(happyCamerasData()) as never,
    )
  })

  it('renders activity stats including formatted recording-storage bytes', async () => {
    renderTab()
    expect(await screen.findByText('Events today')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('1000')).toBeInTheDocument()
    expect(screen.getByText('777')).toBeInTheDocument()
    // recordings_bytes_total 987654321 -> formatBytes -> "941.9 MiB"
    expect(screen.getByText('941.9 MiB')).toBeInTheDocument()
  })

  it('renders a per-camera row with formatted bytes and honest null', async () => {
    renderTab()
    const front = await screen.findByTestId('surveillance-activity-row-FrontDoor')
    expect(within(front).getByText('FrontDoor')).toBeInTheDocument()
    expect(within(front).getByText('42')).toBeInTheDocument()
    // 123456789 -> "117.7 MiB"
    expect(within(front).getByText('117.7 MiB')).toBeInTheDocument()
    const garage = screen.getByTestId('surveillance-activity-row-Garage')
    // null recordings_count -> dash; null recordings_bytes -> formatBytes(null) -> em-dash
    expect(within(garage).getAllByText('—').length).toBeGreaterThanOrEqual(2)
    const zero = screen.getByTestId('surveillance-activity-row-ZeroCam')
    // present-but-zero bytes -> "0 B" (genuine zero), NOT em-dash (unknown)
    expect(within(zero).getByText('0 B')).toBeInTheDocument()
  })

  it('shows the collector-has-not-run empty state when data_available is false', async () => {
    vi.mocked(useSurveillanceCameras).mockReturnValue(
      makeCamerasResult({
        cameras: [],
        events_today: null,
        events_total_all: null,
        recordings_total: null,
        recordings_bytes_total: null,
        data_available: false,
      }) as never,
    )
    renderTab()
    expect(await screen.findByTestId('surveillance-activity-unavailable')).toBeInTheDocument()
  })

  it('renders DSM syslog lines and scopes the query to the synology regex expr', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(
      makeLogsResult({ lines: [makeLine({ message: 'DSM kernel oops' })] }) as never,
    )
    renderTab()
    const body = await screen.findByTestId('logs-body')
    expect(body.textContent).toContain('DSM kernel oops')
    const calls = vi.mocked(useLogsQuery).mock.calls
    const lastCall = calls[calls.length - 1]!
    // expr carries the regex scope; services CSV (4th arg) is EMPTY.
    expect(lastCall[0]).toBe('service:~"synology-.*"')
    expect(lastCall[3]).toBe('')
  })

  it('renders the no_lines empty state for a sparse stream', async () => {
    vi.mocked(useLogsQuery).mockReturnValue(makeLogsResult({ lines: [] }) as never)
    renderTab()
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
  })

  it('renders the unavailable banner on a 502 logs error', async () => {
    const err = new ApiError({
      status: 502,
      code: 'upstream_unavailable',
      message: 'logs backend unavailable',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useLogsQuery).mockReturnValue(makeLogsResult({ error: err }) as never)
    renderTab()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })
})
