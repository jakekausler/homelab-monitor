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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CronDetail } from '@/components/crons/CronDetail'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mock hooks
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useGetCron: vi.fn(),
  useUpdateCron: vi.fn(() => ({
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
  })),
  useHideCron: vi.fn(() => ({
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
  })),
  usePreviewSavedCron: vi.fn(() => ({ isLoading: false, error: null, data: { runs: [] } })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: (s: string | null) => (s ? `abs:${s}` : 'never'),
  formatRelative: (s: string | null) => (s ? `rel:${s}` : 'never'),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

// ---------------------------------------------------------------------------
// Imports that must come AFTER vi.mock declarations
// ---------------------------------------------------------------------------

import { useGetCron, useHideCron, useUpdateCron } from '@/api/crons'
import { toast } from 'sonner'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FP = 'a'.repeat(64)

const baseCron = {
  fingerprint: FP,
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
  hidden_at: null as string | null,
  soft_deleted_at: null as string | null,
  source_path: null as string | null,
  wrapper_last_seen_at: null,
  last_discovered_at: null as string | null,
}

const baseState = null

function makeGetCronResult(overrides: Partial<typeof baseCron> = {}, state = baseState) {
  return {
    isLoading: false,
    error: null,
    data: { cron: { ...baseCron, ...overrides }, state },
  }
}

// ---------------------------------------------------------------------------
// Router wrapper
// ---------------------------------------------------------------------------

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
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CronDetail', () => {
  beforeEach(() => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult() as unknown as ReturnType<typeof useGetCron>,
    )
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  // 1. Renders cron name in header
  it('renders cron name in header', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
  })

  // 2. Renders all 4 panel headings
  it('renders all 4 panel headings', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Heartbeat state')).toBeInTheDocument()
    expect(screen.getByText('Disk source')).toBeInTheDocument()
    expect(screen.getByText('Monitoring policy')).toBeInTheDocument()
    expect(screen.getByText('Actions')).toBeInTheDocument()
  })

  // 3. Shows "Remote" badge when source_path === null
  it('shows Remote badge in header when source_path is null', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Remote')).toBeInTheDocument()
  })

  // 4. Hides "Remote" badge when source_path is present
  it('hides Remote badge when source_path is present', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ source_path: '/etc/cron.d/backup' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByText('Remote')).toBeNull()
  })

  // 5. Shows "Hidden" badge when hidden_at !== null
  it('shows Hidden badge in header when hidden_at is set', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ hidden_at: '2026-05-10T00:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Hidden')).toBeInTheDocument()
  })

  // 6. Hides "Hidden" badge when not hidden
  it('hides Hidden badge when hidden_at is null', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByText('Hidden')).toBeNull()
  })

  // 6b. Shows Soft-deleted badge in header when soft_deleted_at is set
  it('shows Soft-deleted badge in header when soft_deleted_at is set', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ soft_deleted_at: '2026-05-12T00:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByTestId('soft-deleted-badge')).toBeInTheDocument()
  })

  // 6c. Hides Soft-deleted badge when soft_deleted_at is null
  it('hides Soft-deleted badge when soft_deleted_at is null', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByTestId('soft-deleted-badge')).toBeNull()
  })

  // 6d. Disk source panel Soft-deleted row shows relative time when set
  it('Soft-deleted row in Disk source panel shows relative time when soft_deleted_at is set', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ soft_deleted_at: '2026-05-12T00:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('rel:2026-05-12T00:00:00Z')).toBeInTheDocument()
  })

  // 7. Shows remote banner inside Disk source panel when source_path === null
  it('shows remote banner inside Disk source panel when source_path is null', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByTestId('remote-banner')).toBeInTheDocument()
  })

  // 8. Hides remote banner when source_path is present
  it('hides remote banner when source_path is present', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ source_path: '/etc/cron.d/backup' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByTestId('remote-banner')).toBeNull()
  })

  // 9. Hide button click triggers useHideCron mutateAsync + toast.success("Cron hidden")
  it('Hide button click calls useHideCron mutateAsync and shows toast.success', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(undefined)
    vi.mocked(useHideCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useHideCron>)
    renderInRouter(<CronDetail fingerprint={FP} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: /Hide/i }))
    expect(mutateAsync).toHaveBeenCalledTimes(1)
    expect(toast.success).toHaveBeenCalledWith('Cron hidden')
  })

  // 10. Hide button shows toast.error on mutateAsync rejection
  it('Hide button shows toast.error when mutateAsync rejects', async () => {
    const mutateAsync = vi.fn().mockRejectedValue(new Error('Server error'))
    vi.mocked(useHideCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useHideCron>)
    renderInRouter(<CronDetail fingerprint={FP} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: /Hide/i }))
    expect(toast.error).toHaveBeenCalledWith('Hide failed')
  })

  // 11. Unhide button calls useUpdateCron with {hidden_at: null} + toast.success
  it('Unhide button calls useUpdateCron with hidden_at: null and shows toast.success', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(undefined)
    vi.mocked(useUpdateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateCron>)
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ hidden_at: '2026-05-10T00:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: /Unhide/i }))
    expect(mutateAsync).toHaveBeenCalledWith({ hidden_at: null })
    expect(toast.success).toHaveBeenCalledWith('Cron restored')
  })

  // 12. Install heartbeat button is disabled
  it('Install heartbeat button is disabled', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByRole('button', { name: /Install heartbeat wrapper/i })).toBeDisabled()
  })

  // 13. Renders loading state when detail.isLoading is true
  it('renders loading paragraph when detail.isLoading is true', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={FP} />)
    expect(await screen.findByText('Loading cron…')).toBeInTheDocument()
  })

  // 17b. Renders error state when detail.error is set
  it('renders error alert when detail.error is set', async () => {
    vi.mocked(useGetCron).mockReturnValue({
      isLoading: false,
      error: { message: 'Not found' } as Error,
      data: undefined,
    } as unknown as ReturnType<typeof useGetCron>)
    renderInRouter(<CronDetail fingerprint={FP} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Not found')
  })

  // 17c. handleSave catch branch shows toast.error on rejection
  it('handleSave shows toast.error when update mutateAsync rejects', async () => {
    const mutateAsync = vi.fn().mockRejectedValue(new Error('Save failed'))
    vi.mocked(useUpdateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateCron>)
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    const saveButton = screen.getByRole('button', { name: /Save changes/i })
    await userEvent.setup().click(saveButton)
    expect(toast.error).toHaveBeenCalledWith('Update failed')
  })

  // 16. handleUnhide catch branch shows toast.error on rejection
  it('handleUnhide shows toast.error when update mutateAsync rejects', async () => {
    const mutateAsync = vi.fn().mockRejectedValue(new Error('Restore failed'))
    vi.mocked(useUpdateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateCron>)
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ hidden_at: '2026-05-10T00:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await userEvent.setup().click(await screen.findByRole('button', { name: /Unhide/i }))
    expect(toast.error).toHaveBeenCalledWith('Restore failed')
  })

  // 18. Last discovered field renders when last_discovered_at is non-null
  it('Last discovered field renders when last_discovered_at is non-null', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ last_discovered_at: '2026-05-10T12:00:00Z' }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText(/rel:2026-05-10T12:00:00Z/)).toBeInTheDocument()
  })

  // 19. Last discovered field shows em dash when last_discovered_at is null
  it('Last discovered field shows em dash when last_discovered_at is null', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // The Row component renders the "Last discovered" label and the em dash in the value span
    const rows = screen.getAllByText(/Last discovered/)
    expect(rows.length).toBeGreaterThan(0)
    // Check that somewhere in the document there's an em dash near the label
    const contentArea = rows[0]!.parentElement!
    expect(contentArea).toHaveTextContent('—')
  })
})
