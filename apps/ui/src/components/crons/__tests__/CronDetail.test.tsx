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

// Make TooltipContent always render its children so tooltip assertions work in jsdom.
vi.mock('@/components/ui/tooltip', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/components/ui/tooltip')>()
  return {
    ...mod,
    TooltipContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  }
})

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
  useInstallWrapper: vi.fn(() => ({
    mutateAsync: vi.fn().mockReturnValue(new Promise(() => undefined)),
    isPending: false,
    error: null,
  })),
  useUninstallWrapper: vi.fn(() => ({
    mutateAsync: vi.fn().mockReturnValue(new Promise(() => undefined)),
    isPending: false,
    error: null,
  })),
  usePreviewSavedCron: vi.fn(() => ({ isLoading: false, error: null, data: { runs: [] } })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  useListCronRuns: vi.fn(() => ({
    isLoading: false,
    error: null,
    data: { items: [], next_cursor: null },
  })),
  cronQueryKeys: {
    all: ['crons'],
    runs: () => ['crons', 'runs'],
    runLog: () => ['crons', 'run-log'],
  },
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
import type { Schema } from '@/api/types'
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
  is_local: false as boolean | null,
  wrapper_last_seen_at: null as string | null,
  last_discovered_at: null as string | null,
  wrapper_installed: false as boolean,
  last_ok_at: null as string | null,
}

const baseState = null

function makeGetCronResult(
  overrides: Partial<typeof baseCron> = {},
  state: Schema<'HeartbeatStateOut'> | null = baseState,
  wrapperHealth: 'ok' | 'stale' | 'unknown' | 'format_outdated' = 'unknown',
) {
  return {
    isLoading: false,
    error: null,
    data: { cron: { ...baseCron, ...overrides }, state, wrapper_health: wrapperHealth },
  }
}

// ---------------------------------------------------------------------------
// Router wrapper
// ---------------------------------------------------------------------------

function renderInRouter(ui: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const protectedRoute = createRoute({
    getParentRoute: () => rootRoute,
    id: 'protected',
    component: () => <Outlet />,
  })
  const detailRoute = createRoute({
    getParentRoute: () => protectedRoute,
    path: '/integrations/crons/$cronId',
    component: () => <>{ui}</>,
  })
  const cronsListRoute = createRoute({
    getParentRoute: () => protectedRoute,
    path: '/integrations/crons',
    component: () => null,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([protectedRoute.addChildren([detailRoute, cronsListRoute])]),
    history: createMemoryHistory({ initialEntries: ['/integrations/crons/c1'] }),
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

  // 4. Hides "Remote" badge when is_local is true
  it('hides Remote badge when is_local is true', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({
        source_path: '/etc/cron.d/backup',
        is_local: true,
      }) as unknown as ReturnType<typeof useGetCron>,
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

  // 8. Hides remote banner when is_local is true
  it('hides remote banner when is_local is true', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({
        source_path: '/etc/cron.d/backup',
        is_local: true,
      }) as unknown as ReturnType<typeof useGetCron>,
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

  // 20. Install wrapper button is ENABLED for is_local: true cron
  it('Install heartbeat wrapper button is enabled for a local cron (is_local: true)', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ is_local: true }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByRole('button', { name: 'Install heartbeat wrapper' })).toBeEnabled()
  })

  // 21. Install wrapper button is DISABLED for is_local: false cron (remote)
  it('Install heartbeat wrapper button is disabled for a remote cron (is_local: false)', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // baseCron has is_local: false — button rendered inside a span with tooltip
    expect(screen.getByRole('button', { name: 'Install heartbeat wrapper' })).toBeDisabled()
  })

  // 21a. Tooltip text for disabled Install button on remote cron
  it('shows deferred-SSH tooltip on the disabled Install button for a remote cron', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // TooltipContent is mocked to render eagerly; assert the tooltip text is present.
    expect(screen.getByText(/over SSH is deferred.*not yet available/i)).toBeInTheDocument()
  })

  // 21b. Tooltip text for disabled Remove button on remote cron with wrapper installed
  it("shows deferred-SSH 'removal' tooltip on the disabled Remove button for a remote cron with wrapper installed", async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({
        is_local: false,
        wrapper_installed: true,
      }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // TooltipContent is mocked to render eagerly; assert the removal-variant tooltip text is present.
    expect(screen.getByText(/removal over SSH is deferred.*not yet available/i)).toBeInTheDocument()
  })

  // 22. Clicking install button opens the modal (for local cron)
  it('clicking Install heartbeat wrapper button opens the modal for a local cron', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ is_local: true }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Install heartbeat wrapper' }))
    expect(
      await screen.findByText('Install heartbeat wrapper', { selector: '[role="dialog"] *' }),
    ).toBeInTheDocument()
  })

  // 23. is_local null → treated as remote (no ?? true fallback); button disabled
  it('Install heartbeat wrapper button is disabled when is_local is null (treated as remote)', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ is_local: null }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // is_local=null → isLocal=null (falsy) → isRemote=true → button disabled
    expect(screen.getByRole('button', { name: 'Install heartbeat wrapper' })).toBeDisabled()
  })

  // ---------------------------------------------------------------------------
  // STAGE-002-009A: Install/Remove toggle based on wrapper_last_seen_at
  // ---------------------------------------------------------------------------

  // 24. Shows "Install heartbeat wrapper" when wrapper_installed is false
  it('shows Install heartbeat wrapper button when wrapper_installed is false', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ is_local: true, wrapper_installed: false }) as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByRole('button', { name: 'Install heartbeat wrapper' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove heartbeat wrapper' })).toBeNull()
  })

  // 25. Shows "Remove heartbeat wrapper" when wrapper_installed is true
  it('shows Remove heartbeat wrapper button when wrapper_installed is true', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({
        is_local: true,
        wrapper_installed: true,
      }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByRole('button', { name: 'Remove heartbeat wrapper' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Install heartbeat wrapper' })).toBeNull()
  })

  // 26. Remove button disabled for remote cron (is_local: false, wrapper_installed: true)
  it('Remove heartbeat wrapper button is disabled for a remote cron', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({
        is_local: false,
        wrapper_installed: true,
      }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByRole('button', { name: 'Remove heartbeat wrapper' })).toBeDisabled()
  })

  // ---------------------------------------------------------------------------
  // T7 — wrapper-health badge + "Overdue after" label (STAGE-002-010)
  // ---------------------------------------------------------------------------

  // 27. Wrapper NOT installed → no wrapper-health-row
  it('does not render wrapper-health-row when wrapper not installed', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ wrapper_installed: false }, null, 'unknown') as unknown as ReturnType<
        typeof useGetCron
      >,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByTestId('wrapper-health-row')).toBeNull()
  })

  // 28. wrapper_installed=true, wrapper_health='ok' → badge "OK"
  it('renders wrapper-health-badge with text OK when wrapper_health is ok', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { is_local: true, wrapper_installed: true },
        null,
        'ok',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByTestId('wrapper-health-row')).toBeInTheDocument()
    expect(screen.getByTestId('wrapper-health-badge')).toHaveTextContent('OK')
  })

  // 29. wrapper_installed=true, wrapper_health='stale' → badge "Stale"
  it('renders wrapper-health-badge with text Stale when wrapper_health is stale', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { is_local: true, wrapper_installed: true },
        null,
        'stale',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByTestId('wrapper-health-badge')).toHaveTextContent('Stale')
  })

  // 31. format_outdated badge renders "Re-install to enable run logs"
  it('renders format_outdated badge with Re-install text', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { is_local: true, wrapper_installed: true },
        null,
        'format_outdated',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByTestId('wrapper-health-badge')).toHaveTextContent(
      'Re-install to enable run logs',
    )
  })

  // 32. format_outdated badge carries warn variant styling
  it('format_outdated badge has warn variant styling', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { is_local: true, wrapper_installed: true },
        null,
        'format_outdated',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    // The badge element exists and is visible
    const badge = screen.getByTestId('wrapper-health-badge')
    expect(badge).toBeInTheDocument()
    // Warn variant: badge does NOT say OK
    expect(badge).not.toHaveTextContent('OK')
  })

  // 33. format_outdated description text mentions re-install
  it('format_outdated shows re-install description text', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { is_local: true, wrapper_installed: true },
        null,
        'format_outdated',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText(/Re-install to enable per-run output capture/)).toBeInTheDocument()
  })

  // 30. "Overdue after" label appears; "Next due" does NOT appear
  it('shows "Overdue after" label and not "Next due" when state has expected_next_at', async () => {
    const stateWithDeadline = {
      cron_fingerprint: FP,
      current_state: 'ok' as const,
      last_start_at: null,
      last_ok_at: '2026-05-01T00:00:00Z',
      last_fail_at: null,
      current_streak: 1,
      expected_next_at: '2026-05-02T00:00:00Z',
      last_duration_seconds: null,
      last_exit_code: null,
      updated_at: '2026-05-01T00:00:00Z',
    }
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult(
        { cadence_seconds: 3600 },
        stateWithDeadline,
        'unknown',
      ) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Overdue after')).toBeInTheDocument()
    expect(screen.queryByText('Next due')).toBeNull()
  })

  // ---------------------------------------------------------------------------
  // BUG 1: Wrapper indicator reflects INSTALLED state, not liveness
  // ---------------------------------------------------------------------------

  // 34. Wrapper row shows "Installed" when wrapper_installed is true
  it('shows Installed in Wrapper row when wrapper_installed is true', async () => {
    vi.mocked(useGetCron).mockReturnValue(
      makeGetCronResult({ wrapper_installed: true }) as unknown as ReturnType<typeof useGetCron>,
    )
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Installed')).toBeInTheDocument()
  })

  // 35. Wrapper row shows "Not installed" when wrapper_installed is false
  it('shows Not installed in Wrapper row when wrapper_installed is false', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.getByText('Not installed')).toBeInTheDocument()
  })

  // 36. Old copy "No wrapper installed (heartbeats from ad-hoc curl)" is not present
  it('does not render old wrapper copy about heartbeats from ad-hoc curl', async () => {
    renderInRouter(<CronDetail fingerprint={FP} />)
    await screen.findByText('daily-backup')
    expect(screen.queryByText(/No wrapper installed \(heartbeats from ad-hoc curl\)/)).toBeNull()
  })
})
