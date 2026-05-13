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
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronDetailPage } from '@/routes/inventory/CronDetailPage'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

vi.mock('@/api/crons', () => ({
  useGetCron: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  useUpdateCron: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useHideCron: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  usePreviewSavedCron: vi.fn(() => ({ isLoading: false, error: null, data: { runs: [] } })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: () => 'never',
  formatRelative: () => 'never',
}))

function renderPage(fingerprint?: string) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const inventoryRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/inventory',
    component: () => <Outlet />,
  })
  const cronsRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons',
    component: () => <div>Crons list</div>,
  })
  const cronDetailRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons/$fingerprint',
    component: CronDetailPage,
  })
  const noIdRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: CronDetailPage,
  })
  const initialPath = fingerprint ? `/inventory/crons/${fingerprint}` : '/'
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      inventoryRoute.addChildren([cronsRoute, cronDetailRoute]),
      noIdRoute,
    ]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
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

describe('CronDetailPage', () => {
  it('renders back link to crons list', async () => {
    renderPage('c1')
    expect(await screen.findByRole('link', { name: /Back to crons/i })).toBeInTheDocument()
  })

  it('renders missing cron fingerprint message when no fingerprint param', async () => {
    renderPage()
    expect(await screen.findByText(/Missing cron fingerprint/i)).toBeInTheDocument()
  })

  it('renders CronDetail (not found state) when fingerprint is present', async () => {
    renderPage('c1')
    expect(await screen.findByText(/Cron not found/i)).toBeInTheDocument()
  })
})
