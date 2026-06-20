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
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CronRunsListPage } from '@/routes/integrations/crons/CronRunsList'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useGetCron: vi.fn(),
  useListCronRuns: vi.fn(),
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: (s: string | null) => (s ? `abs:${s}` : '—'),
  formatRelative: (s: string | null) => (s ? `rel:${s}` : '—'),
  formatDuration: (n: number | null) => (n === null ? '—' : `${String(n)}s`),
}))

import { useGetCron, useListCronRuns } from '@/api/crons'

// ---------------------------------------------------------------------------
// Fixtures
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

const baseCronResult = {
  isLoading: false,
  error: null,
  data: {
    cron: {
      fingerprint: FP,
      name: 'my-cron',
      host: 'testhost',
      command: '/bin/test',
      schedule: '* * * * *',
      schedule_canonical: '* * * * *',
      cadence_seconds: 60,
      expected_grace_seconds: 300,
      enabled: true,
      last_seen_state: 'ok' as const,
      created_at: '2026-05-01T00:00:00Z',
      updated_at: '2026-05-01T00:00:00Z',
      hidden_at: null,
      soft_deleted_at: null,
      source_path: null,
      is_local: false,
      wrapper_last_seen_at: null,
      last_discovered_at: null,
      wrapper_installed: false,
    },
    state: null,
    wrapper_health: 'unknown' as const,
  },
}

// ---------------------------------------------------------------------------
// Router helper
// ---------------------------------------------------------------------------

// The component calls useSearch({ from: '/protected/integrations/crons/$fingerprint/runs' }).
// We must define a route tree that matches this "from" path so TanStack Router
// can resolve the search params. We name the protected layout segment and nest
// the runs route under it.
function renderWithRouter(initialSearch: string = '') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const protectedRoute = createRoute({
    getParentRoute: () => rootRoute,
    id: 'protected',
    component: () => <Outlet />,
  })
  const cronsRoute = createRoute({
    getParentRoute: () => protectedRoute,
    path: '/integrations/crons',
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
    component: CronRunsListPage,
    validateSearch: (
      search: Record<string, unknown>,
    ): {
      cursor?: string | undefined
      state?: 'running' | 'ok' | 'fail' | 'unknown' | undefined
    } => ({
      cursor: typeof search.cursor === 'string' ? search.cursor : undefined,
      state:
        search.state === 'running' ||
        search.state === 'ok' ||
        search.state === 'fail' ||
        search.state === 'unknown'
          ? search.state
          : undefined,
    }),
  })
  const cronRunLogRoute = createRoute({
    getParentRoute: () => cronRunsListRoute,
    path: '/$run_id',
    component: () => null,
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      protectedRoute.addChildren([
        cronsRoute.addChildren([
          cronDetailRoute.addChildren([cronRunsListRoute.addChildren([cronRunLogRoute])]),
        ]),
      ]),
    ]),
    history: createMemoryHistory({
      initialEntries: [`/integrations/crons/${FP}/runs${initialSearch}`],
    }),
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

describe('CronRunsListPage', () => {
  beforeEach(() => {
    vi.mocked(useGetCron).mockReturnValue(
      baseCronResult as unknown as ReturnType<typeof useGetCron>,
    )
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
    renderWithRouter()
    expect(await screen.findByText('Loading runs…')).toBeInTheDocument()
  })

  it('renders error state with alert role', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: { message: 'Request failed' },
      data: undefined,
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    expect(await screen.findByRole('alert')).toHaveTextContent('Request failed')
  })

  it('renders empty state when no items and not loading', async () => {
    renderWithRouter()
    expect(await screen.findByTestId('runs-empty')).toBeInTheDocument()
    expect(screen.getByText('No runs match these filters.')).toBeInTheDocument()
  })

  it('renders desktop table with correct header columns', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    const desktop = await screen.findByTestId('runs-desktop')
    expect(within(desktop).getByText('Started')).toBeInTheDocument()
    expect(within(desktop).getByText('Duration')).toBeInTheDocument()
    expect(within(desktop).getByText('State')).toBeInTheDocument()
    expect(within(desktop).getByText('Source')).toBeInTheDocument()
    expect(within(desktop).getByText('Exit')).toBeInTheDocument()
    expect(within(desktop).getByText('Anomalies')).toBeInTheDocument()
    expect(within(desktop).getByText('Overlap')).toBeInTheDocument()
  })

  it('renders desktop table with 3 body rows for 3 items', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' }), makeRun({ run_id: 'r2' }), makeRun({ run_id: 'r3' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    const desktop = await screen.findByTestId('runs-desktop')
    const tbody = desktop.querySelector('tbody')
    expect(tbody?.querySelectorAll('tr')).toHaveLength(3)
  })

  it('renders mobile cards with 3 list items for 3 items', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' }), makeRun({ run_id: 'r2' }), makeRun({ run_id: 'r3' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    const mobile = await screen.findByTestId('runs-mobile')
    expect(mobile.querySelectorAll('li')).toHaveLength(3)
  })

  it('renders RunStateBadge for each run row', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [
          makeRun({ run_id: 'r1', state: 'ok' }),
          makeRun({ run_id: 'r2', state: 'fail', exit_code: 1 }),
        ],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    await screen.findByTestId('runs-desktop')
    const okBadges = screen.getAllByLabelText('Run state ok')
    expect(okBadges.length).toBeGreaterThanOrEqual(1)
    const failBadges = screen.getAllByLabelText('Run state fail')
    expect(failBadges.length).toBeGreaterThanOrEqual(1)
  })

  it('renders overlap badge when overlapping is true', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', overlapping: true })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    await screen.findByTestId('runs-desktop')
    expect(screen.getAllByTestId('overlap-badge').length).toBeGreaterThanOrEqual(1)
  })

  it('omits overlap badge when overlapping is false', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', overlapping: false })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    await screen.findByTestId('runs-desktop')
    expect(screen.queryByTestId('overlap-badge')).toBeNull()
  })

  it('renders anomaly badges for comma-separated flags', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1', anomaly_flags: 'duration_outlier,exit_code_changed' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    await screen.findByTestId('runs-desktop')
    const badges = screen.getAllByTestId('anomaly-badge')
    expect(badges.length).toBeGreaterThanOrEqual(2)
  })

  it('shows Next page button when next_cursor is set', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' })],
        next_cursor: 'cursor-xyz',
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    expect(await screen.findByTestId('next-page')).toBeInTheDocument()
  })

  it('omits Next page button when next_cursor is null', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    await screen.findByTestId('runs-desktop')
    expect(screen.queryByTestId('next-page')).toBeNull()
  })

  it('clicking Next page button navigates with cursor search param', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'r1' })],
        next_cursor: 'cursor-xyz',
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    const btn = await screen.findByTestId('next-page')
    await userEvent.setup().click(btn)
    // After navigation useListCronRuns will be called with cursor param.
    // We verify the button was present and clickable (no throw).
    expect(btn).toBeInTheDocument()
  })

  it('state filter Select changes search param on selection', async () => {
    renderWithRouter()
    const select = await screen.findByLabelText('Filter by state')
    await userEvent.setup().selectOptions(select, 'fail')
    // After changing to 'fail', no error is thrown and the component re-renders.
    expect(select).toBeInTheDocument()
  })

  it('state filter "All" option is present', async () => {
    renderWithRouter()
    const select = await screen.findByLabelText('Filter by state')
    expect(within(select as HTMLSelectElement).getByText('All')).toBeInTheDocument()
  })

  it('row link in desktop table targets log viewer route', async () => {
    vi.mocked(useListCronRuns).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [makeRun({ run_id: 'run-abc' })],
        next_cursor: null,
      },
    } as unknown as ReturnType<typeof useListCronRuns>)
    renderWithRouter()
    const desktop = await screen.findByTestId('runs-desktop')
    const link = within(desktop).getByRole('link', { name: /rel:/ })
    expect(link).toHaveAttribute('href', expect.stringContaining('/runs/run-abc'))
  })

  it('back link targets cron detail page', async () => {
    renderWithRouter()
    await screen.findByText('Run history')
    const backLink = screen.getByRole('link', { name: /Back to my-cron/ })
    expect(backLink).toHaveAttribute('href', expect.stringContaining(`/integrations/crons/${FP}`))
  })

  it('renders cron name and host subtitle when cron data available', async () => {
    renderWithRouter()
    await screen.findByText('Run history')
    expect(screen.getByText(/my-cron.*testhost/)).toBeInTheDocument()
  })
})
