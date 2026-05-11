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
  useCreateCron: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

import { useListCrons } from '@/api/crons'

const sampleCron = {
  id: 'c1',
  name: 'daily-backup',
  host: 'host-a',
  command: '/opt/backup.sh',
  schedule: '0 4 * * *',
  schedule_canonical: '0 4 * * *',
  cadence_seconds: 0,
  expected_grace_seconds: 300,
  integration_mode: 'observe' as const,
  enabled: true,
  last_seen_state: 'ok' as const,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  archived_at: null,
}

function renderPage(search = '') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const protectedRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/protected',
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
  })
  const cronDetailRoute = createRoute({
    getParentRoute: () => cronsRoute,
    path: '/$cronId',
    component: () => null,
  })
  const path = `/protected/inventory/crons${search}`
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
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
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

  it('shows Add cron button and opens modal on click', async () => {
    vi.mocked(useListCrons).mockReturnValue({
      isLoading: false,
      error: null,
      data: { items: [], total: 0 },
    } as unknown as ReturnType<typeof useListCrons>)
    renderPage()
    const addBtn = await screen.findByRole('button', { name: /Add cron/i })
    await userEvent.setup().click(addBtn)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders pagination when total exceeds items length', async () => {
    const items = Array.from({ length: 5 }, (_, i) => ({
      ...sampleCron,
      id: `c${i}`,
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
      id: `c${i}`,
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
      id: `c${i}`,
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
})
