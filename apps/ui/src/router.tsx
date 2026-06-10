import { createRoute, createRouter, redirect } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'

import { apiClient } from '@/api/client'
import { queryKeys } from '@/api/queries'
import { parseColumnsParam } from '@/api/logs'
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
import { DockerIntegrationPage } from '@/routes/integrations/DockerIntegrationPage'
import { ContainerPage } from '@/routes/integrations/ContainerPage'
import { ContainerOverviewTab } from '@/routes/integrations/ContainerOverviewTab'
import { ContainerProbesTab } from '@/routes/integrations/ContainerProbesTab'
import { ContainerLogsTab } from '@/routes/integrations/ContainerLogsTab'
import { ContainerActionsTab } from '@/routes/integrations/ContainerActionsTab'
import { LogsExplorerPage } from '@/routes/logs/LogsExplorerPage'
import { LogsLayout } from '@/routes/logs/LogsLayout'
import { ModelsDebugPage } from '@/routes/logs/ModelsDebugPage'
import { SignaturesTab } from '@/routes/logs/SignaturesTab'
import { SignatureDetailPage } from '@/routes/logs/SignatureDetailPage'
import { SilenceAllowlistTab } from '@/routes/logs/SilenceAllowlistTab'
import { UserRulesTab } from '@/routes/logs/UserRulesTab'
import { SettingsLayout } from '@/routes/settings/SettingsLayout'
import { SettingsLogsPage } from '@/routes/settings/SettingsLogsPage'
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

/**
 * STAGE-004-012A — parse the `services` URL param (CSV of `<source_type>:<service>`
 * entries) into ServiceIdentity[]. Splits each entry on the FIRST ':'. Drops
 * malformed/empty entries. Absent param → undefined (so the key is omitted).
 */
export function parseServicesParam(
  raw: unknown,
): { source_type: string; service: string }[] | undefined {
  // If it's already an array of objects, return as-is (TanStack Router caches the result)
  if (
    Array.isArray(raw) &&
    raw.length > 0 &&
    typeof raw[0] === 'object' &&
    raw[0] !== null &&
    'source_type' in raw[0] &&
    'service' in raw[0]
  ) {
    return raw as { source_type: string; service: string }[]
  }

  const csv =
    typeof raw === 'string'
      ? raw
      : Array.isArray(raw)
        ? (raw as unknown[]).filter((s): s is string => typeof s === 'string').join(',')
        : undefined
  if (csv === undefined) return undefined
  const out: { source_type: string; service: string }[] = []
  for (const part of csv.split(',')) {
    const entry = part.trim()
    if (entry.length === 0) continue
    const idx = entry.indexOf(':')
    if (idx <= 0) continue // no colon, or empty source_type
    const source_type = entry.slice(0, idx)
    const service = entry.slice(idx + 1)
    if (service.length === 0) continue // empty service
    out.push({ source_type, service })
  }
  return out.length > 0 ? out : undefined
}

const logsLayoutRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/logs',
  component: LogsLayout,
})

const logsIndexRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: '/',
  beforeLoad: () => {
    // Preserve Explorer deep-link search params across the redirect.
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({ to: '/logs/query', search: (prev) => prev })
  },
})

const logsQueryRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'query',
  component: LogsExplorerPage,
  // STAGE-004-010 — URL is the source of truth. `start`+`end` (ISO, custom) take
  // precedence over `since` (preset token); `q` is the plain-text search term.
  // STAGE-004-012A — `services` is now a CSV of `<source_type>:<service>` identities.
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    q?: string | undefined
    logsql?: string | undefined
    since?: string | undefined
    start?: string | undefined
    end?: string | undefined
    services?: { source_type: string; service: string }[] | undefined
    columns?: string[] | undefined
  } => ({
    q: typeof search.q === 'string' ? search.q : undefined,
    logsql: typeof search.logsql === 'string' ? search.logsql : undefined,
    since: typeof search.since === 'string' ? search.since : undefined,
    start: typeof search.start === 'string' ? search.start : undefined,
    end: typeof search.end === 'string' ? search.end : undefined,
    services: parseServicesParam(search.services),
    columns: parseColumnsParam(search.columns),
  }),
})

const logsSignaturesRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'signatures',
  component: SignaturesTab,
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    service?: string | undefined
    status?: 'active' | 'suppressed' | 'expected' | undefined
    label_q?: string | undefined
  } => ({
    service: typeof search.service === 'string' ? search.service : undefined,
    status:
      search.status === 'active' || search.status === 'suppressed' || search.status === 'expected'
        ? search.status
        : undefined,
    label_q: typeof search.label_q === 'string' ? search.label_q : undefined,
  }),
})

const logsSignatureDetailRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'signatures/$templateHash/$serviceKey',
  component: SignatureDetailPage,
})

const logsModelsDebugRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'models-debug',
  component: ModelsDebugPage,
  validateSearch: (search: Record<string, unknown>): { model?: string | undefined } => ({
    ...(typeof search.model === 'string' ? { model: search.model } : {}),
  }),
})

const logsSilenceAllowlistRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'silence-allowlist',
  component: SilenceAllowlistTab,
})

const logsUserRulesRoute = createRoute({
  getParentRoute: () => logsLayoutRoute,
  path: 'user-rules',
  component: UserRulesTab,
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
  // STAGE-004-008 — custom range is CLIENT-SIDE narrowing (no backend call),
  // but reflected in the URL per the locked decision. Bounded to the run window.
  validateSearch: (
    search: Record<string, unknown>,
  ): { start?: string | undefined; end?: string | undefined } => ({
    start: typeof search.start === 'string' ? search.start : undefined,
    end: typeof search.end === 'string' ? search.end : undefined,
  }),
})

const settingsLayoutRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/settings',
  component: SettingsLayout,
})

const settingsIndexRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: '/',
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({ to: '/settings/logs' })
  },
})

const settingsLogsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: 'logs',
  component: SettingsLogsPage,
})

const dockerIntegrationRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/integrations/docker',
  component: DockerIntegrationPage,
})

// NEW: parent route hosts the shared header + tab strip; children render in <Outlet>.
const containerPageRoute = createRoute({
  getParentRoute: () => protectedLayoutRoute,
  path: '/integrations/docker/containers/$name',
  component: ContainerPage,
})

const containerIndexRoute = createRoute({
  getParentRoute: () => containerPageRoute,
  path: '/',
  beforeLoad: ({ params }) => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router redirect objects are thrown by design
    throw redirect({
      to: '/integrations/docker/containers/$name/overview',
      params: { name: params.name },
    })
  },
})

const containerOverviewRoute = createRoute({
  getParentRoute: () => containerPageRoute,
  path: 'overview',
  component: ContainerOverviewTab,
})

const containerProbesRoute = createRoute({
  getParentRoute: () => containerPageRoute,
  path: 'probes',
  component: ContainerProbesTab,
})

const containerLogsRoute = createRoute({
  getParentRoute: () => containerPageRoute,
  path: 'logs',
  component: ContainerLogsTab,
  // STAGE-004-008 — URL is the source of truth for the selected time range.
  // `start`+`end` (ISO, custom) take precedence over `since` (preset token).
  validateSearch: (
    search: Record<string, unknown>,
  ): { since?: string | undefined; start?: string | undefined; end?: string | undefined } => ({
    since: typeof search.since === 'string' ? search.since : undefined,
    start: typeof search.start === 'string' ? search.start : undefined,
    end: typeof search.end === 'string' ? search.end : undefined,
  }),
})

const containerActionsRoute = createRoute({
  getParentRoute: () => containerPageRoute,
  path: 'actions',
  component: ContainerActionsTab,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  protectedLayoutRoute.addChildren([
    indexRoute,
    overviewRoute,
    alertsRoute,
    metricsRoute,
    logsLayoutRoute.addChildren([
      logsIndexRoute,
      logsQueryRoute,
      logsSignaturesRoute,
      logsSignatureDetailRoute,
      logsSilenceAllowlistRoute,
      logsUserRulesRoute,
      logsModelsDebugRoute,
    ]),
    inventoryLayoutRoute.addChildren([
      inventoryIndexRoute,
      cronsListRoute,
      cronDetailRoute,
      cronRunsListRoute,
      cronRunLogViewerRoute,
    ]),
    settingsLayoutRoute.addChildren([settingsIndexRoute, settingsLogsRoute]),
    dockerIntegrationRoute,
    containerPageRoute.addChildren([
      containerIndexRoute,
      containerOverviewRoute,
      containerProbesRoute,
      containerLogsRoute,
      containerActionsRoute,
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
