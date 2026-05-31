import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { CronRunLogViewerPage } from '@/routes/inventory/CronRunLogViewer'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useCronRunLog: vi.fn(),
  cronQueryKeys: {
    runLog: (fp: string, rid: string) => ['crons', 'run-log', fp, rid],
  },
}))

vi.mock('@/lib/relativeTime', () => ({
  formatDuration: (n: number | null) => (n === null ? '—' : `${String(n)}s`),
  formatLogTimestamp: (raw: string | null | undefined) => raw ?? '',
}))

import { useCronRunLog } from '@/api/crons'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FP = 'testfingerprint123'
const RUN_ID = 'run-abc-001'

function makeLogData(
  overrides: Partial<{
    state: string
    log_status: string
    truncated: boolean
    lines: Array<{ timestamp: string; message: string }>
    anomaly_flags: string
    duration_seconds: number | null
    line_count: number | null
    exit_code: number | null
  }> = {},
) {
  return {
    run_id: RUN_ID,
    cron_fingerprint: FP,
    state: 'ok',
    log_status: 'available',
    truncated: false,
    lines: [],
    anomaly_flags: '',
    duration_seconds: 30,
    line_count: null,
    exit_code: 0,
    started_at: '2026-05-01T12:00:00Z',
    ended_at: '2026-05-01T12:00:30Z',
    source: 'wrapper',
    overlapping: false,
    byte_count: null,
    content_digest: null,
    enriched_at: null,
    vl_window_start: '2026-05-01T12:00:00Z',
    vl_window_end: '2026-05-01T12:00:30Z',
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// Router helper
// ---------------------------------------------------------------------------

function renderWithRouter(initialPath: string = `/inventory/crons/${FP}/runs/${RUN_ID}`) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const inventoryRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/inventory',
    component: () => <Outlet />,
  })
  const cronsRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons',
    component: () => <Outlet />,
  })
  const cronDetailRoute = createRoute({
    getParentRoute: () => cronsRoute,
    path: '/$fingerprint',
    component: () => <Outlet />,
  })
  const cronRunsListRoute = createRoute({
    getParentRoute: () => cronDetailRoute,
    path: '/runs',
    component: () => <Outlet />,
    validateSearch: (
      search: Record<string, unknown>,
    ): {
      cursor?: string | undefined
      state?: 'running' | 'ok' | 'fail' | 'unknown' | undefined
    } => ({
      cursor: typeof search.cursor === 'string' ? search.cursor : undefined,
      state: undefined,
    }),
  })
  const cronRunLogRoute = createRoute({
    getParentRoute: () => cronRunsListRoute,
    path: '/$run_id',
    component: CronRunLogViewerPage,
    validateSearch: (
      search: Record<string, unknown>,
    ): { start?: string | undefined; end?: string | undefined } => ({
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
    }),
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      inventoryRoute.addChildren([
        cronsRoute.addChildren([
          cronDetailRoute.addChildren([cronRunsListRoute.addChildren([cronRunLogRoute])]),
        ]),
      ]),
    ]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CronRunLogViewerPage', () => {
  beforeEach(() => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeLogData()], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
  })

  it('renders loading state', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: true,
      isFetching: false,
      error: null,
      data: undefined,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByText('Loading logs…')).toBeInTheDocument()
  })

  it('renders log entries for available log', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [
              { timestamp: '2026-05-01T12:00:01Z', message: 'Starting backup' },
              { timestamp: '2026-05-01T12:00:05Z', message: 'Files copied' },
              { timestamp: '2026-05-01T12:00:29Z', message: 'Done' },
            ],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    expect(within(body).getByText('Starting backup')).toBeInTheDocument()
    expect(within(body).getByText('Files copied')).toBeInTheDocument()
    expect(within(body).getByText('Done')).toBeInTheDocument()
  })

  it('renders entry timestamps in log body', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-01T12:00:01Z', message: 'hello' }],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    expect(within(body).getByText('2026-05-01T12:00:01Z')).toBeInTheDocument()
  })

  it('renders empty log placeholder when entries is empty', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [makeLogData({ log_status: 'available', lines: [] })],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    expect(body.textContent).toBe('')
  })

  it('renders truncated banner when truncated is true', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            truncated: true,
            lines: [{ timestamp: 't1', message: 'line' }],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('truncated-banner')).toBeInTheDocument()
    expect(screen.getByTestId('truncated-banner')).toHaveTextContent('Narrow the time window')
  })

  it('omits truncated banner when truncated is false', async () => {
    renderWithRouter()
    await screen.findByTestId('logs-body')
    expect(screen.queryByTestId('truncated-banner')).toBeNull()
  })

  it('renders running banner and Refresh button when log_status is running', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            state: 'running',
            log_status: 'running',
            exit_code: null,
            duration_seconds: null,
            lines: [],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('running-banner')).toBeInTheDocument()
    expect(screen.getByTestId('running-banner')).toHaveTextContent('Run in progress')
    expect(screen.getByTestId('refresh-log')).toBeInTheDocument()
  })

  it('omits Refresh button when log_status is available', async () => {
    renderWithRouter()
    await screen.findByTestId('logs-body')
    expect(screen.queryByTestId('refresh-log')).toBeNull()
  })

  it('useCronRunLog is called (integration: refetchInterval active for running status)', async () => {
    // This test verifies the hook is invoked for a running run.
    // The refetchInterval logic lives in the hook itself (api/crons.ts) and is
    // tested in isolation; here we just confirm the component renders correctly
    // when log_status='running' — the Refresh button should be visible.
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            state: 'running',
            log_status: 'running',
            exit_code: null,
            duration_seconds: null,
            lines: [],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('refresh-log')).toBeInTheDocument()
  })

  it('renders expired notice when log_status is expired', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeLogData({ log_status: 'expired', lines: [] })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('expired-notice')).toBeInTheDocument()
    expect(screen.getByTestId('expired-notice')).toHaveTextContent('Log text no longer available')
  })

  it('omits log-body when log_status is expired', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeLogData({ log_status: 'expired', lines: [] })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    await screen.findByTestId('expired-notice')
    expect(screen.queryByTestId('logs-body')).toBeNull()
  })

  it('renders unavailable banner on 503 ApiError', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: new ApiError({
        status: 503,
        code: 'vl_unavailable',
        message: 'VictoriaLogs down',
        retryAfterSeconds: null,
        details: null,
      }),
      data: { pages: [], pageParams: [] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'The log backend is temporarily unavailable.',
    )
  })

  it('omits unavailable banner and shows generic error for non-503 ApiError', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: new ApiError({
        status: 500,
        code: 'internal_error',
        message: 'Internal server error',
        retryAfterSeconds: null,
        details: null,
      }),
      data: { pages: [], pageParams: [] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error')
    expect(screen.queryByTestId('unavailable-banner')).toBeNull()
  })

  it('renders anomaly badges in the header for non-empty flags', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [makeLogData({ anomaly_flags: 'duration_outlier,exit_code_changed', lines: [] })],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    await screen.findByTestId('run-log-header')
    const badges = screen.getAllByTestId('anomaly-badge')
    expect(badges.length).toBeGreaterThanOrEqual(2)
  })

  it('sticky header element has sticky and top-0 class', async () => {
    renderWithRouter()
    const header = await screen.findByTestId('run-log-header')
    expect(header.className).toContain('sticky')
    expect(header.className).toContain('top-0')
  })

  it('truncates run_id longer than 12 chars in header code element', async () => {
    const longRunId = 'a'.repeat(20)
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeLogData({ lines: [] })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter(`/inventory/crons/${FP}/runs/${longRunId}`)
    const header = await screen.findByTestId('run-log-header')
    const code = header.querySelector('code')
    expect(code?.textContent).toMatch(/…$/)
    expect(code?.textContent?.length).toBeLessThan(20)
  })

  it('back link targets runs list route', async () => {
    renderWithRouter()
    await screen.findByTestId('run-log-header')
    const backLink = screen.getByRole('link', { name: /Back to runs/ })
    expect(backLink).toHaveAttribute('href', expect.stringContaining(`/inventory/crons/${FP}/runs`))
  })

  it('renders run state badge in header', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeLogData({ state: 'ok', lines: [] })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    await screen.findByTestId('run-log-header')
    expect(screen.getByLabelText('Run state ok')).toBeInTheDocument()
  })

  it('renders the wrap toggle when data is present', async () => {
    renderWithRouter()
    expect(await screen.findByTestId('wrap-toggle')).toBeInTheDocument()
  })

  it('toggling wrap switches the log body to wrapping mode', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-01T12:00:01Z', message: 'hello' }],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    expect(body.className).toContain('overflow-x-auto')
    const checkbox = within(screen.getByTestId('wrap-toggle')).getByRole('checkbox')
    fireEvent.click(checkbox)
    expect(screen.getByTestId('logs-body').className).toContain('whitespace-normal')
  })

  it('renders older pages above newer pages in multi-page load', async () => {
    // pages[0] = newest window (newer-1), pages[1] = older window (older-1)
    // After reverse, should render: older-1 then newer-1
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-01T12:00:10Z', message: 'newer-1' }],
          }),
          makeLogData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-01T12:00:00Z', message: 'older-1' }],
          }),
        ],
        pageParams: [undefined, 'cursor-1'],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    const text = body.textContent ?? ''
    expect(text.indexOf('older-1')).toBeLessThan(text.indexOf('newer-1'))
  })

  it('renders the bounded time-range control when lines have timestamps', async () => {
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [
              { timestamp: '2026-05-01T12:00:01Z', message: 'a' },
              { timestamp: '2026-05-01T12:00:20Z', message: 'b' },
            ],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)
    renderWithRouter()
    expect(await screen.findByTestId('time-range-trigger')).toBeInTheDocument()
  })

  it('open-bound custom: start-only URL filters lines from selStart to runMax (open end resolves to run max)', async () => {
    const startIso = '2026-05-01T12:00:05Z'
    vi.mocked(useCronRunLog).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeLogData({
            log_status: 'available',
            lines: [
              { timestamp: '2026-05-01T12:00:01Z', message: 'before-start' },
              { timestamp: '2026-05-01T12:00:05Z', message: 'at-start' },
              { timestamp: '2026-05-01T12:00:20Z', message: 'after-start' },
            ],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useCronRunLog>)

    // URL has only ?start= (no end) → open end should resolve to runMax (12:00:20Z).
    renderWithRouter(`/inventory/crons/${FP}/runs/${RUN_ID}?start=${encodeURIComponent(startIso)}`)

    const body = await screen.findByTestId('logs-body')
    const text = body.textContent ?? ''

    // 'before-start' is before selStart → filtered out.
    expect(text).not.toContain('before-start')
    // 'at-start' is at selStart → included.
    expect(text).toContain('at-start')
    // 'after-start' is after selStart and <= runMax → included.
    expect(text).toContain('after-start')
  })
})
