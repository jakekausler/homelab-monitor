import { createRoute, createRouter, redirect } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'
import { queryKeys } from '@/api/queries'
import type { Schema } from '@/api/types'
import type { RunSearchSchema } from '@/routes/inventory/types'
import { Route as rootRoute } from '@/routes/__root'
import { AlertsPage } from '@/routes/Alerts'
import { LoginPage } from '@/routes/Login'
import { MetricsPage } from '@/routes/Metrics'
import { OverviewPage } from '@/routes/Overview'
import { InventoryLayout } from '@/routes/inventory/Inventory'
import { CronsListPage } from '@/routes/inventory/CronsList'
import { CronDetailPage } from '@/routes/inventory/CronDetailPage'
import { CronRunsListPage } from '@/routes/inventory/CronRunsList'
import { CronRunLogViewerPage } from '@/routes/inventory/CronRunLogViewer'
import { AppShell } from '@/components/AppShell'
import { ErrorDisplay } from '@/components/ErrorDisplay'

type MeResponse = Schema<'MeResponse'>

/**
 * Auth guard: ensures the user is logged in by hitting GET /api/auth/me
 * via the queryClient cache. On 401, throws a redirect to /login. The
 * cached result lets navigation between protected routes skip the network.
 */
export async function ensureAuthenticated(queryClient: QueryClient): Promise<MeResponse> {
  const cached = queryClient.getQueryData<MeResponse | null>(queryKeys.currentUser)
  if (cached) return cached
  const data = await queryClient.fetchQuery({
    queryKey: queryKeys.currentUser,
    queryFn: async (): Promise<MeResponse | null> => {
      const result = await apiClient.GET('/api/auth/me')
      if (result.response.status === 401) return null
      if (result.error !== undefined) throw new Error('me-failed')
      return result.data ?? null
    },
    retry: false,
    staleTime: 30_000,
  })
  if (data === null) {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({ to: '/login' })
  }
  return data
}

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: LoginPage,
})

const protectedLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'protected',
  beforeLoad: async ({ context }) => {
    await ensureAuthenticated(context.queryClient)
  },
  component: AppShell,
})

const indexRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/',
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({ to: '/overview' })
  },
})

const overviewRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/overview',
  component: OverviewPage,
})

const alertsRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/alerts',
  component: AlertsPage,
})

const metricsRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/metrics',
  component: MetricsPage,
})

const inventoryLayoutRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/inventory',
  component: InventoryLayout,
})

const inventoryIndexRoute = createRoute({
  getParentRoute: () => inventoryLayoutRoute,
  path: '/',
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({
      to: '/inventory/crons',
      search: {
        page: 1,
        page_size: 100,
        host: undefined,
        state: undefined,
        q: undefined,
        include_hidden: false,
        include_soft_deleted: false,
      },
    })
  },
})

const cronsListRoute = createRoute({
  getParentRoute: () => inventoryLayoutRoute,
  path: '/crons',
  component: CronsListPage,
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    page: number
    page_size: number
    host?: string | undefined
    state?: 'unknown' | 'running' | 'ok' | 'failed' | 'late' | undefined
    q?: string | undefined
    wrapper_installed?: boolean | undefined
    include_hidden: boolean
    include_soft_deleted: boolean
  } => ({
    page: typeof search.page === 'number' ? search.page : 1,
    page_size: typeof search.page_size === 'number' ? search.page_size : 100,
    host: typeof search.host === 'string' ? search.host : undefined,
    state:
      search.state === 'unknown' ||
      search.state === 'running' ||
      search.state === 'ok' ||
      search.state === 'failed' ||
      search.state === 'late'
        ? search.state
        : undefined,
    q: typeof search.q === 'string' ? search.q : undefined,
    wrapper_installed:
      typeof search.wrapper_installed === 'boolean' ? search.wrapper_installed : undefined,
    include_hidden: typeof search.include_hidden === 'boolean' ? search.include_hidden : false,
    include_soft_deleted:
      typeof search.include_soft_deleted === 'boolean' ? search.include_soft_deleted : false,
  }),
})

const cronDetailRoute = createRoute({
  getParentRoute: () => inventoryLayoutRoute,
  path: '/crons/$fingerprint',
  component: CronDetailPage,
})

const cronRunsListRoute = createRoute({
  getParentRoute: () => inventoryLayoutRoute,
  path: '/crons/$fingerprint/runs',
  component: CronRunsListPage,
  validateSearch: (search: Record<string, unknown>): RunSearchSchema => ({
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

const cronRunLogViewerRoute = createRoute({
  getParentRoute: () => inventoryLayoutRoute,
  path: '/crons/$fingerprint/runs/$run_id',
  component: CronRunLogViewerPage,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  protectedLayoutRoute.addChildren([
    indexRoute,
    overviewRoute,
    alertsRoute,
    metricsRoute,
    inventoryLayoutRoute.addChildren([
      inventoryIndexRoute,
      cronsListRoute,
      cronDetailRoute,
      cronRunsListRoute,
      cronRunLogViewerRoute,
    ]),
  ]),
])

export function createAppRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: 'intent',
    defaultErrorComponent: ErrorDisplay,
  })
}

export type AppRouter = ReturnType<typeof createAppRouter>

declare module '@tanstack/react-router' {
  interface Register {
    router: AppRouter
  }
}
