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

import { RecentRunsPanel } from '@/components/crons/RecentRunsPanel'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mock hooks
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useListCronRuns: vi.fn(),
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: (s: string | null) => (s ? `abs:${s}` : '—'),
  formatRelative: (s: string | null) => (s ? `rel:${s}` : '—'),
  formatDuration: (n: number | null) => (n === null ? '—' : `${String(n)}s`),
}))

import { useListCronRuns } from '@/api/crons'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FP = 'testfingerprint123'

function makeRun(
  overrides: Partial<{
    run_id: string
    state: string
    started_at: string
    duration_seconds: number | null
    exit_code: number | null
    anomaly_flags: string
    source: string
    overlapping: boolean
  }> = {},
) {
  return {
    run_id: 'run-001',
    cron_fingerprint: FP,
    state: 'ok',
    started_at: '2026-05-01T12:00:00Z',
    ended_at: '2026-05-01T12:00:30Z',
    duration_seconds: 30,
    exit_code: 0,
    source: 'wrapper',
    anomaly_flags: '',
    overlapping: false,
    line_count: null,
    byte_count: null,
    content_digest: null,
    enriched_at: null,
    vl_window_start: '2026-05-01T12:00:00Z',
    vl_window_end: '2026-05-01T12:00:30Z',
    ...overrides,
  }
}

function renderInRouter(ui: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  // Register the /inventory/crons/$fingerprint/runs route so Link can resolve it
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
    component: () => null,
  })
  const cronRunLogRoute = createRoute({
    getParentRoute: () => cronRunsListRoute,
    path: '/$run_id',
    component: () => null,
  })
  const panelRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/panel',
    component: () => <>{ui}</>,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      panelRoute,
      inventoryRoute.addChildren([
        cronsRoute.addChildren([
          cronDetailRoute.addChildren([cronRunsListRoute.addChildren([cronRunLogRoute])]),
        ]),
      ]),
    ]),
    history: createMemoryHistory({ initialEntries: ['/panel'] }),
  })
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RecentRunsPanel', () => {
  beforeEach(() => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [], next_cursor: null },
    } as unknown as ReturnType<typeof useListCronRuns>)
  })

  it('renders loading state', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByText('Loading runs…')).toBeInTheDocument()
  })

  it('renders error state with alert role', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: { message: 'Network error' },
      data: undefined,
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Network error')
  })

  it('renders empty state when items is empty', async () => {
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByTestId('recent-runs-empty')).toBeInTheDocument()
    expect(screen.getByText('No runs recorded yet.')).toBeInTheDocument()
  })

  it('renders run rows when items are present', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [
          makeRun({ run_id: 'r1', state: 'ok' }),
          makeRun({ run_id: 'r2', state: 'fail' }),
          makeRun({ run_id: 'r3', state: 'unknown' }),
        ],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    const list = await screen.findByTestId('recent-runs-list')
    const items = list.querySelectorAll('li')
    expect(items).toHaveLength(3)
  })

  it('renders exactly 5 run rows when 5 items provided', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: Array.from({ length: 5 }, (_, i) =>
          makeRun({ run_id: `r${String(i)}`, state: 'ok' }),
        ),
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    const list = await screen.findByTestId('recent-runs-list')
    expect(list.querySelectorAll('li')).toHaveLength(5)
  })

  it('renders View all runs link with correct href', async () => {
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    const link = await screen.findByTestId('view-all-runs-link')
    expect(link).toHaveAttribute('href', expect.stringContaining(`/inventory/crons/${FP}/runs`))
  })

  it('renders anomaly badge when anomaly_flags is non-empty (single flag)', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', anomaly_flags: 'duration_outlier' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByTestId('anomaly-badge')).toBeInTheDocument()
    expect(screen.getByTestId('anomaly-badge')).toHaveTextContent('duration_outlier')
  })

  it('omits anomaly badge when anomaly_flags is empty string', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', anomaly_flags: '' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    await screen.findByTestId('recent-runs-list')
    expect(screen.queryByTestId('anomaly-badge')).toBeNull()
  })

  it('renders count label for multiple anomaly flags', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', anomaly_flags: 'a,b,c' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByTestId('anomaly-badge')).toHaveTextContent('3 anomalies')
  })

  it('renders exit code when exit_code is non-null', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', exit_code: 1, state: 'fail' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByText('exit 1')).toBeInTheDocument()
  })

  it('omits exit code row when exit_code is null', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', exit_code: null, state: 'running' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    await screen.findByTestId('recent-runs-list')
    expect(screen.queryByText(/exit /)).toBeNull()
  })

  it('renders RunStateBadge for each run state', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [
          makeRun({ run_id: 'r1', state: 'ok' }),
          makeRun({ run_id: 'r2', state: 'fail' }),
          makeRun({ run_id: 'r3', state: 'running', exit_code: null }),
          makeRun({ run_id: 'r4', state: 'unknown', exit_code: null }),
        ],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    await screen.findByTestId('recent-runs-list')
    expect(screen.getByLabelText('Run state ok')).toBeInTheDocument()
    expect(screen.getByLabelText('Run state fail')).toBeInTheDocument()
    expect(screen.getByLabelText('Run state running')).toBeInTheDocument()
    expect(screen.getByLabelText('Run state unknown')).toBeInTheDocument()
  })

  it('renders the Recent runs panel heading', async () => {
    renderInRouter(<RecentRunsPanel fingerprint={FP} />)
    expect(await screen.findByText('Recent runs')).toBeInTheDocument()
  })
})
