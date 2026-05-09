import { createRoute, createRouter, redirect, type AnyRoute } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'
import { queryKeys } from '@/api/queries'
import { Route as rootRoute } from '@/routes/__root'
import { AlertsPage } from '@/routes/Alerts'
import { LoginPage } from '@/routes/Login'
import { MetricsPage } from '@/routes/Metrics'
import { OverviewPage } from '@/routes/Overview'
import { AppShell } from '@/components/AppShell'
import { ErrorDisplay } from '@/components/ErrorDisplay'

import type { components } from '@/api/schema'

type MeResponse = components['schemas']['MeResponse']

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

const routeTree: AnyRoute = rootRoute.addChildren([
  loginRoute,
  protectedLayoutRoute.addChildren([indexRoute, overviewRoute, alertsRoute, metricsRoute]),
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
