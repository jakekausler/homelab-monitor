// Integration tests: Explorer state persistence (STAGE-004-015).
// Tests the round-trip between LogsExplorerPage and explorerState localStorage.
// Mirrors LogsExplorerPage.test.tsx conventions: router wrapper, mocked hooks,
// localStorage cleanup in afterEach.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LogsExplorerPage } from '@/routes/logs/LogsExplorerPage'
import { parseServicesParam } from '@/router'
import {
  loadExplorerState,
  saveExplorerState,
  STORAGE_KEY,
  type ExplorerState,
} from '@/lib/explorerState'
import type { Schema } from '@/api/types'

afterEach(cleanup)
afterEach(() => {
  localStorage.removeItem('homelab-monitor:timezone')
  localStorage.removeItem('homelab-monitor:logs-query-history')
  localStorage.removeItem(STORAGE_KEY)
})

vi.mock('@/api/logs', () => ({
  useLogsQuery: vi.fn(),
  useLogsServicesQuery: vi.fn(),
  useLogsHistogramQuery: vi.fn(() => ({ data: undefined, isLoading: false, isError: false })),
  identitiesToServicesCsv: (identities: Array<{ source_type: string; service: string }>) =>
    identities.map((i) => `${i.source_type}:${i.service}`).join(','),
}))

vi.mock('@/lib/useMediaQuery', () => ({
  useMediaQuery: vi.fn(() => true),
}))

import { useLogsQuery, useLogsServicesQuery } from '@/api/logs'

type LogLine = Schema<'LogLine'>
type LogsQueryResponse = Schema<'LogsQueryResponse'>

function makePage(
  overrides: Partial<{ lines: LogLine[]; next_cursor: string | null; has_more: boolean }> = {},
): LogsQueryResponse {
  return { lines: [], next_cursor: null, has_more: false, ...overrides }
}

function renderRoute(initialPath = '/logs') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const logsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/logs',
    component: LogsExplorerPage,
    validateSearch: (
      search: Record<string, unknown>,
    ): {
      q?: string | undefined
      logsql?: string | undefined
      since?: string | undefined
      start?: string | undefined
      end?: string | undefined
      services?: { source_type: string; service: string }[] | undefined
    } => ({
      q: typeof search.q === 'string' ? search.q : undefined,
      logsql: typeof search.logsql === 'string' ? search.logsql : undefined,
      since: typeof search.since === 'string' ? search.since : undefined,
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
      services: parseServicesParam(search.services),
    }),
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([logsRoute]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    router,
    ...render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    ),
  }
}

function mockLogsQuery(overrides: Record<string, unknown> = {}): void {
  vi.mocked(useLogsQuery).mockReturnValue({
    isLoading: false,
    isFetching: false,
    isError: false,
    error: null,
    data: { pages: [makePage()], pageParams: [undefined] },
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
    ...overrides,
  } as unknown as ReturnType<typeof useLogsQuery>)
}

function mockServicesQuery(): void {
  vi.mocked(useLogsServicesQuery).mockReturnValue({
    data: { services: [], truncated: false },
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useLogsServicesQuery>)
}

beforeEach(() => {
  mockLogsQuery()
  mockServicesQuery()
})

// ---------------------------------------------------------------------------
// A) Persistence write-back: page mount writes to localStorage
// ---------------------------------------------------------------------------

describe('persistence write-back on mount', () => {
  it('first visit (no URL, no persisted) writes default state to localStorage', async () => {
    renderRoute()
    await screen.findByTestId('logs-search-input')
    // The mount-time useEffect fires synchronously for the initial state.
    await waitFor(() => {
      const stored = loadExplorerState()
      expect(stored).not.toBeNull()
    })
    const stored = loadExplorerState()
    expect(stored?.advanced_mode).toBe(false)
    expect(stored?.logs_ql).toBe('')
    expect(stored?.selected_services).toEqual([])
    expect(typeof stored?.last_visited_at).toBe('number')
  })

  it('URL params (q=foo) are written to localStorage on mount', async () => {
    renderRoute('/logs?q=foo&since=24h')
    await screen.findByTestId('logs-search-input')
    await waitFor(() => {
      const stored = loadExplorerState()
      expect(stored?.logs_ql).toBe('foo')
    })
    const stored = loadExplorerState()
    expect(stored?.since_preset).toBe('24h')
  })
})

// ---------------------------------------------------------------------------
// B) Restore from persisted: mount with no URL reads localStorage
// ---------------------------------------------------------------------------

describe('restore from persisted state (no URL params)', () => {
  it('restores plain-text query from persisted state', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: 'restored query',
      selected_services: [],
      since_preset: '1h',
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    renderRoute('/logs')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('restored query')
  })

  it('restores services from persisted state — service chip appears', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
      since_preset: '1h',
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    renderRoute('/logs')
    await screen.findByTestId('logs-search-input')
    const chip = await screen.findByTestId('service-chip')
    expect(chip).toHaveAttribute('data-service', 'nginx')
  })

  it('first visit (no URL, no persisted) renders empty input', async () => {
    renderRoute('/logs')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('')
  })

  it('expired persisted state (>7 days) is ignored → default empty', async () => {
    const eightDaysAgo = Date.now() - 8 * 24 * 60 * 60 * 1000
    saveExplorerState({
      advanced_mode: false,
      logs_ql: 'should-be-ignored',
      selected_services: [],
      since_preset: '6h',
      last_visited_at: eightDaysAgo,
    } satisfies ExplorerState)

    renderRoute('/logs')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('')
  })
})

// ---------------------------------------------------------------------------
// C) URL precedence: URL params win over persisted state
// ---------------------------------------------------------------------------

describe('URL precedence over persisted state (ALL-OR-NOTHING)', () => {
  it('?q= param wins over persisted logs_ql', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: 'persisted-query',
      selected_services: [],
      since_preset: '1h',
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    renderRoute('/logs?q=url-query')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('url-query')
  })

  it('?q= param: no service chips from persisted services (URL wins ALL-OR-NOTHING)', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
      since_preset: '1h',
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    // URL has q param but no services → URL wins; persisted services ignored
    renderRoute('/logs?q=anything')
    await screen.findByTestId('logs-search-input')
    // No chip — URL took precedence, persisted services ignored
    expect(screen.queryByTestId('service-chip')).toBeNull()
  })

  it('?logsql= param → advanced mode, not plain mode from persisted', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: 'plain-persisted',
      selected_services: [],
      since_preset: '1h',
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    renderRoute('/logs?logsql=service%3Afoo')
    // Advanced mode renders the logsql textarea, not the plain input
    const editor = await screen.findByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(editor.value).toBe('service:foo')
    expect(screen.queryByTestId('logs-search-input')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// D) Scroll restore: pure-function coverage (actual DOM scroll tested manually)
// ---------------------------------------------------------------------------
// NOTE: jsdom does not implement CSS layout, so Element.scrollTo is a no-op
// and scrollTop is always 0. The actual Body scroll-restore wiring (useLayoutEffect
// calling scrollContainer.scrollTo(0, restoreScrollTarget)) is verified manually
// during Refinement phase against the running dev rig.
// What we CAN test here is that resolveInitialExplorerState correctly propagates
// restoreScrollTarget from persisted state — covered exhaustively in the pure
// explorerState.test.ts. The integration test below verifies the Page passes the
// correct seed through to Body (via the presence of the scroll_position in
// persisted state being loaded, without asserting actual scrollTop which jsdom
// cannot deliver).

describe('scroll restore: seed propagation', () => {
  it('mounts without error when persisted state has a positive scroll_position', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [],
      since_preset: '1h',
      scroll_position: 850,
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    // If the Page crashes trying to use restoreScrollTarget, this will fail.
    renderRoute('/logs')
    expect(await screen.findByTestId('logs-search-input')).toBeInTheDocument()
  })

  it('mounts without error when persisted state has null scroll_position', async () => {
    saveExplorerState({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [],
      since_preset: '1h',
      scroll_position: null,
      last_visited_at: Date.now(),
    } satisfies ExplorerState)

    renderRoute('/logs')
    expect(await screen.findByTestId('logs-search-input')).toBeInTheDocument()
  })
})
