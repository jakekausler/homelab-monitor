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
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronDetail } from '@/components/crons/CronDetail'

afterEach(cleanup)

vi.mock('@/api/crons', () => ({
  useGetCron: vi.fn(),
  useUpdateCron: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSoftDeleteCron: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  usePreviewSavedCron: vi.fn(() => ({ isLoading: false, error: null, data: { runs: [] } })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: (s: string | null) => (s ? `abs:${s}` : 'never'),
  formatRelative: (s: string | null) => (s ? `rel:${s}` : 'never'),
}))

import { useGetCron } from '@/api/crons'

const sampleCron = {
  fingerprint: 'a'.repeat(64),
  name: 'daily-backup',
  host: 'host-a',
  command: '/opt/backup.sh',
  schedule: '0 4 * * *',
  schedule_canonical: '0 4 * * *',
  cadence_seconds: 0,
  expected_grace_seconds: 300,
  enabled: true,
  last_seen_state: 'ok' as const,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  hidden_at: null,
  source_path: null,
  wrapper_installed_at: null,
}

function renderInRouter(ui: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const detailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/crons/$cronId',
    component: () => <>{ui}</>,
  })
  const inventoryRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/inventory',
    component: () => null,
  })
  const cronsListRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons',
    component: () => null,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([detailRoute, inventoryRoute.addChildren([cronsListRoute])]),
    history: createMemoryHistory({ initialEntries: ['/crons/c1'] }),
  })
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('CronDetail', () => {
  it('shows loading text while fetching', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText(/Loading cron/i)).toBeInTheDocument()
  })

  it('shows error message when fetch fails', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: { message: 'Not found' } as Error,
      data: undefined,
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Not found')
  })

  it('shows "Cron not found" when data is null', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: null,
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText(/Cron not found/i)).toBeInTheDocument()
  })

  it('renders cron name and Archive button for active cron', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: sampleCron, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Archive/i })).toBeInTheDocument()
  })

  it('renders Restore button for archived cron', async () => {
    const hidden = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: hidden, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByRole('button', { name: /Restore/i })).toBeInTheDocument()
  })

  it('shows no pings message when state is null', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: sampleCron, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText(/No pings received yet/i)).toBeInTheDocument()
  })

  it('renders heartbeat state rows when state is present', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        cron: sampleCron,
        state: {
          current_state: 'ok',
          current_streak: 5,
          last_ok_at: '2026-05-11T03:00:00Z',
          last_fail_at: null,
          expected_next_at: '2026-05-12T04:00:00Z',
        },
      },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText('Streak')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
  })

  it('opens delete modal when Archive button is clicked', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: sampleCron, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    const deleteBtn = await screen.findByRole('button', { name: /Archive/i })
    await userEvent.setup().click(deleteBtn)
    expect(await screen.findByText(/Archive cron\?/i)).toBeInTheDocument()
  })

  it('shows cadence-based schedule preview message when schedule is null', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: { ...sampleCron, schedule: null }, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText(/Cadence-based/i)).toBeInTheDocument()
  })

  it('shows archived badge for hidden cron', async () => {
    const hidden = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: hidden, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText('archived')).toBeInTheDocument()
  })

  it('renders command in subtitle', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: sampleCron, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    expect(await screen.findByText('/opt/backup.sh')).toBeInTheDocument()
  })

  it('calls updateCron mutateAsync when save form is submitted', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(sampleCron)
    const { useUpdateCron } = await import('@/api/crons')
    vi.mocked(useUpdateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateCron>)
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: sampleCron, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    await screen.findByText('daily-backup')
    await userEvent.setup().click(screen.getByRole('button', { name: /Save changes/i }))
    expect(mutateAsync).toHaveBeenCalledTimes(1)
  })

  it('calls updateCron with hidden_at: null when Restore is clicked', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(sampleCron)
    const { useUpdateCron } = await import('@/api/crons')
    vi.mocked(useUpdateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateCron>)
    const hidden = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { cron: hidden, state: null },
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={'a'.repeat(64)} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: /Restore/i }))
    expect(mutateAsync).toHaveBeenCalledWith({ hidden_at: null })
  })
})
