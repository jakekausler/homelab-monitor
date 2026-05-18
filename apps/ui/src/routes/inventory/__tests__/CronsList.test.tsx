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

import { CronsListPage } from '@/routes/inventory/CronsList'

afterEach(cleanup)

vi.mock('@/api/crons', () => ({
  useListCrons: vi.fn(),
  useDiscoverNow: vi.fn(),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { useListCrons, useDiscoverNow } from '@/api/crons'
import type { components } from '@/api/schema'
import { toast } from 'sonner'
import { beforeEach } from 'vitest'
import { ApiError } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'

type CronOut = components['schemas']['CronOut']

beforeEach(() => {
  vi.mocked(useDiscoverNow).mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof useDiscoverNow>)
})

const sampleCron: CronOut = {
  fingerprint: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
  name: 'daily-backup',
  host: 'host-a',
  command: '/opt/backup.sh',
  schedule: '0 4 * * *',
  schedule_canonical: '0 4 * * *',
  cadence_seconds: 0,
  expected_grace_seconds: 300,
  enabled: true,
  is_local: true,
  last_seen_state: 'ok' as const,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  hidden_at: null,
  source_path: null,
  wrapper_last_seen_at: null,
  last_discovered_at: null,
  soft_deleted_at: null,
  wrapper_installed: false,
}

function renderPage(search = '') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const protectedRoute = createRoute({
    getParentRoute: () => rootRoute,
    id: 'protected',
    component: () => <Outlet />,
  })
  const inventoryRoute = createRoute({
    getParentRoute: () => protectedRoute,
    path: '/inventory',
    component: () => <Outlet />,
  })
  const cronsRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons',
    component: CronsListPage,
    validateSearch: (search: Record<string, unknown>) => ({
      page: search.page !== undefined ? Number(search.page) : undefined,
      page_size: search.page_size !== undefined ? Number(search.page_size) : undefined,
      q: typeof search.q === 'string' ? search.q : undefined,
      state:
        typeof search.state === 'string'
          ? (search.state as 'unknown' | 'running' | 'ok' | 'failed' | 'late')
          : undefined,
      host: typeof search.host === 'string' ? search.host : undefined,
      include_hidden: search.include_hidden === true || search.include_hidden === 'true',
      wrapper_installed:
        search.wrapper_installed === true || search.wrapper_installed === 'true'
          ? true
          : search.wrapper_installed === false || search.wrapper_installed === 'false'
            ? false
            : undefined,
    }),
  })
  const cronDetailRoute = createRoute({
    getParentRoute: () => cronsRoute,
    path: '/$cronId',
    component: () => null,
  })
  const path = `/inventory/crons${search}`
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      protectedRoute.addChildren([
        inventoryRoute.addChildren([cronsRoute.addChildren([cronDetailRoute])]),
      ]),
    ]),
    history: createMemoryHistory({ initialEntries: [path] }),
  })
  const qc = new QueryClient()
  return render(
    <TooltipProvider>
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </TooltipProvider>,
  )
}

describe('CronsListPage', () => {
  it('renders loading state', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    expect(await screen.findByText(/Loading crons/i)).toBeInTheDocument()
  })

  it('renders empty hint when no crons', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [], total: 0 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    expect(await screen.findByText(/No crons yet/i)).toBeInTheDocument()
  })

  it('renders cron rows from data', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.getAllByText('host-a').length).toBeGreaterThan(0)
  })

  it('shows error alert when list query fails', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: { message: 'fetch error' } as Error,
      data: undefined,
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('fetch error')
  })

  it('renders pagination when total exceeds items length', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      name: `cron-${i}`,
    }))
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items, total: 50 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    expect(await screen.findByRole('button', { name: /Next/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Previous/i })).toBeInTheDocument()
  })

  it('Previous button is disabled and Next enabled on page 1 with small page_size', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      name: `cron-${i}`,
    }))
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items, total: 50 },
    } as unknown as ReturnType<typeof useListCrons>)
    // Use page_size=5 so lastPage=10, making Next enabled
    renderPage('?page=1&page_size=5')
    expect(await screen.findByRole('button', { name: /Previous/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Next/i })).toBeEnabled()
  })

  it('shows correct page summary text', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      name: `cron-${i}`,
    }))
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items, total: 50 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage('?page=1&page_size=5')
    expect(await screen.findByText(/Page 1 of 10/i)).toBeInTheDocument()
    expect(screen.getByText(/50 crons/i)).toBeInTheDocument()
  })

  it('Next button click navigates to page 2', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      name: `cron-${i}`,
    }))
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items, total: 50 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage('?page=1&page_size=5')
    const next = await screen.findByRole('button', { name: /Next/i })
    await userEvent.setup().click(next)
    // After navigation, page summary text updates to page 2
    expect(await screen.findByText(/Page 2 of 10/i)).toBeInTheDocument()
  })

  it('Previous button click navigates back to page 1', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      name: `cron-${i}`,
    }))
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items, total: 50 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage('?page=2&page_size=5')
    const prev = await screen.findByRole('button', { name: /Previous/i })
    await userEvent.setup().click(prev)
    expect(await screen.findByText(/Page 1 of 10/i)).toBeInTheDocument()
  })

  it('Discover now button is present and enabled initially', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({ found_count: 5, error_count: 0 }),
      isPending: false,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    expect(button).toBeEnabled()
  })

  it('Discover now button shows success toast with found and error counts', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    const mutateAsync = vi.fn().mockResolvedValue({ found_count: 5, error_count: 2 })
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    await userEvent.setup().click(button)
    expect(mutateAsync).toHaveBeenCalledTimes(1)
    expect(toast.success).toHaveBeenCalledWith('Discovery scan complete. 5 crons found — 2 errors.')
  })

  it('Discover now button shows success toast without error count when error_count is 0', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    const mutateAsync = vi.fn().mockResolvedValue({ found_count: 3, error_count: 0 })
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    await userEvent.setup().click(button)
    expect(toast.success).toHaveBeenCalledWith('Discovery scan complete. 3 crons found.')
  })

  it('Discover now button shows 429 throttle toast when response status is 429', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    const mockError = new ApiError({
      status: 429,
      code: 'discover_now_throttled',
      message: 'throttled',
      retryAfterSeconds: 30,
      details: null,
    })
    const mutateAsync = vi.fn().mockRejectedValue(mockError)
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    await userEvent.setup().click(button)
    expect(toast.error).toHaveBeenCalledWith('Discovery scan recently triggered. Retry in 30s.')
  })

  it('Discover now button shows generic error toast on other errors', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    const mutateAsync = vi.fn().mockRejectedValue(new Error('Server error'))
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    await userEvent.setup().click(button)
    expect(toast.error).toHaveBeenCalledWith('Discovery scan failed.')
  })

  it('Discover now button is disabled while mutation is pending', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [sampleCron], total: 1 },
    } as unknown as ReturnType<typeof useListCrons>)
    vi.mocked(useDiscoverNow).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: true,
    } as unknown as ReturnType<typeof useDiscoverNow>)
    renderPage()
    const button = await screen.findByRole('button', { name: /Discover now/i })
    expect(button).toBeDisabled()
  })
})
