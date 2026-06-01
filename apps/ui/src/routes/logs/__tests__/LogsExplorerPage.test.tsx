import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'
import { LogsExplorerPage } from '@/routes/logs/LogsExplorerPage'
import type { Schema } from '@/api/types'

afterEach(cleanup)
afterEach(() => {
  localStorage.removeItem('homelab-monitor:timezone')
})

// Mock the data hook so the route renders without network. We capture the
// (expr, start, end) args to assert the plain-text → LogsQL translation.
vi.mock('@/api/logs', () => ({
  useLogsQuery: vi.fn(),
  useLogsServicesQuery: vi.fn(),
}))

// Force the LogsQlEditor narrow-viewport textarea path. NOTE: LogsExplorerBody
// renders <LogsQlEditor> which calls useMediaQuery('(max-width: 767px)'); a
// false here means "not narrow" → the wide/CodeMirror branch. To keep CM6 out of
// jsdom, return TRUE so the shell renders the plain textarea directly.
// LogsExplorerBody uses the SAME query for mobile-drawer detection — returning true
// means the tests exercise the MOBILE drawer path. To assert the DESKTOP inline sidebar,
// override per-test via vi.mocked(useMediaQuery).mockReturnValue(false).
vi.mock('@/lib/useMediaQuery', () => ({
  useMediaQuery: vi.fn(() => true),
}))

import { useLogsQuery, useLogsServicesQuery } from '@/api/logs'
import { useMediaQuery } from '@/lib/useMediaQuery'

// Typed against the REAL generated schema so a contract change breaks this test
// instead of passing against a stale hand-written shape.
type LogLine = Schema<'LogLine'>
type LogsQueryResponse = Schema<'LogsQueryResponse'>

function makePage(
  overrides: Partial<{ lines: LogLine[]; next_cursor: string | null; has_more: boolean }> = {},
): LogsQueryResponse {
  return {
    lines: [],
    next_cursor: null,
    has_more: false,
    ...overrides,
  }
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
      services?: string[] | undefined
    } => ({
      q: typeof search.q === 'string' ? search.q : undefined,
      logsql: typeof search.logsql === 'string' ? search.logsql : undefined,
      since: typeof search.since === 'string' ? search.since : undefined,
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
      services:
        typeof search.services === 'string'
          ? search.services.split(',').filter((s) => s.length > 0)
          : Array.isArray(search.services)
            ? (search.services as unknown[]).filter((s): s is string => typeof s === 'string')
            : undefined,
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

describe('LogsExplorerPage', () => {
  // Replace the useLogsQuery mock return value. Defaults to a loaded, empty,
  // single-page result; pass overrides for loading/error/data scenarios.
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

  function mockServicesQuery(overrides: Record<string, unknown> = {}): void {
    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: {
        services: [
          { service: 'home-assistant', count: 1204 },
          { service: 'nginx', count: 12 },
        ],
        truncated: false,
      },
      isLoading: false,
      isError: false,
      ...overrides,
    } as unknown as ReturnType<typeof useLogsServicesQuery>)
  }

  beforeEach(() => {
    mockLogsQuery()
    mockServicesQuery()
  })

  it('renders the search input, time-range control, and a log viewer region', async () => {
    renderRoute()
    expect(await screen.findByTestId('logs-search-input')).toBeInTheDocument()
    expect(screen.getByTestId('logs-search-submit')).toBeInTheDocument()
    expect(screen.getByTestId('time-range-trigger')).toBeInTheDocument()
  })

  it('hydrates the input value and translated expr from the URL (?q + ?since)', async () => {
    renderRoute('/logs?q=connection%20refused&since=24h')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('connection refused')
    // The hook is called with the translated expr derived from the committed q.
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([expr]) => expr === '_msg:"connection refused"')).toBe(true)
  })

  it('typing a term + clicking Search fires the query with the translated _msg expr', async () => {
    renderRoute()
    const input = await screen.findByTestId('logs-search-input')
    fireEvent.change(input, { target: { value: 'timeout' } })
    fireEvent.click(screen.getByTestId('logs-search-submit'))
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([expr]) => expr === '_msg:"timeout"')).toBe(true)
  })

  it('empty search uses match-all (omits ?q) → expr is "*"', async () => {
    renderRoute()
    await screen.findByTestId('logs-search-input')
    // No q in the URL and no input → committed text is empty → expr === '*'.
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([expr]) => expr === '*')).toBe(true)
  })

  it('keeps the Clear button visible when input is emptied but a committed filter is still applied', async () => {
    renderRoute('/logs?q=foo&since=1h')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    // Committed filter 'foo' is active — Clear button must be present.
    expect(input.value).toBe('foo')
    expect(screen.queryByTestId('logs-search-clear')).not.toBeNull()
    // User manually deletes all text from the input (live text becomes '').
    fireEvent.change(input, { target: { value: '' } })
    // Clear button must STILL be visible: committed filter 'foo' is still active.
    // Before the fix, the button disappeared here because the condition only
    // checked liveSearchText.length > 0.
    expect(screen.queryByTestId('logs-search-clear')).not.toBeNull()
  })

  it('hydrates a custom range from the URL (?start + ?end) and queries those exact ISO bounds', async () => {
    const start = '2026-05-30T00:00:00.000Z'
    const end = '2026-05-30T06:00:00.000Z'
    renderRoute(`/logs?start=${start}&end=${end}`)
    await screen.findByTestId('logs-search-input')
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([, s, e]) => s === start && e === end)).toBe(true)
  })

  it('does NOT commit live input text to the query until Search is clicked', async () => {
    renderRoute()
    const input = await screen.findByTestId('logs-search-input')
    // Discard calls from the initial render so we only inspect calls caused by
    // typing. (mock.calls accumulates across tests in the suite otherwise.)
    vi.mocked(useLogsQuery).mockClear()
    // Type without submitting.
    fireEvent.change(input, { target: { value: 'foo' } })
    // The live text must never reach the query until the user submits: every
    // post-type call must still use the committed expr ('*'), never '_msg:"foo"'.
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.every(([expr]) => expr !== '_msg:"foo"')).toBe(true)
  })

  it('renders the unavailable state when the backend returns HTTP 502', async () => {
    mockLogsQuery({
      isLoading: false,
      isError: true,
      error: new ApiError({
        status: 502,
        code: 'upstream_unavailable',
        message: 'VictoriaLogs unavailable',
        retryAfterSeconds: null,
        details: null,
      }),
      data: undefined,
    })
    renderRoute()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
    expect(screen.getByTestId('logs-search-input')).toBeInTheDocument()
  })

  it('renders a header error alert (not the unavailable state) for a generic non-502 API error', async () => {
    mockLogsQuery({
      isLoading: false,
      isError: true,
      error: new ApiError({
        status: 500,
        code: 'internal',
        message: 'boom',
        retryAfterSeconds: null,
        details: null,
      }),
      data: undefined,
    })
    renderRoute()
    await screen.findByTestId('logs-search-input')
    // Generic ApiError → Body maps to isError:false + a role="alert" banner in the header.
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
    expect(screen.queryByTestId('unavailable-banner')).toBeNull()
  })

  it('renders the loading state while the query is in flight', async () => {
    mockLogsQuery({ isLoading: true, data: undefined })
    renderRoute()
    expect(await screen.findByText('Loading logs…')).toBeInTheDocument()
  })

  it('toggling Advanced on shows the LogsQL editor; toggling off restores the plain input', async () => {
    renderRoute()
    await screen.findByTestId('logs-search-input')
    // Flip Advanced on.
    const toggleCheckbox = screen.getByTestId('logs-advanced-toggle').querySelector('input')!
    fireEvent.click(toggleCheckbox)
    expect(await screen.findByTestId('logsql-editor-textarea')).toBeInTheDocument()
    expect(screen.queryByTestId('logs-search-input')).toBeNull()
    // Flip Advanced off.
    fireEvent.click(toggleCheckbox)
    expect(await screen.findByTestId('logs-search-input')).toBeInTheDocument()
    expect(screen.queryByTestId('logsql-editor-textarea')).toBeNull()
  })

  it("preserves each mode's text across toggles", async () => {
    renderRoute()
    const plainInput = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    fireEvent.change(plainInput, { target: { value: 'plain-term' } })
    // Switch to advanced, type LogsQL.
    const toggleCheckbox = screen.getByTestId('logs-advanced-toggle').querySelector('input')!
    fireEvent.click(toggleCheckbox)
    const editor = await screen.findByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    fireEvent.change(editor, { target: { value: 'service:home-assistant' } })
    // Back to plain — the plain text is still there.
    fireEvent.click(toggleCheckbox)
    const plainAgain = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(plainAgain.value).toBe('plain-term')
    // Back to advanced — the LogsQL text is still there.
    fireEvent.click(toggleCheckbox)
    const editorAgain = await screen.findByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(editorAgain.value).toBe('service:home-assistant')
  })

  it('advanced mode sends the committed LogsQL as expr RAW (not translated)', async () => {
    renderRoute()
    await screen.findByTestId('logs-search-input')
    const toggleCheckbox = screen.getByTestId('logs-advanced-toggle').querySelector('input')!
    fireEvent.click(toggleCheckbox)
    const editor = await screen.findByTestId('logsql-editor-textarea')
    fireEvent.change(editor, {
      target: { value: 'service:home-assistant AND severity:error' },
    })
    vi.mocked(useLogsQuery).mockClear()
    fireEvent.click(screen.getByTestId('logs-search-submit'))
    const calls = vi.mocked(useLogsQuery).mock.calls
    // RAW: the exact LogsQL string, NOT wrapped in _msg:"…".
    expect(calls.some(([expr]) => expr === 'service:home-assistant AND severity:error')).toBe(true)
    expect(calls.every(([expr]) => !String(expr).startsWith('_msg:'))).toBe(true)
  })

  it('deep-links into advanced mode from ?logsql and queries it raw', async () => {
    renderRoute('/logs?logsql=service%3Afoo')
    const editor = await screen.findByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(editor.value).toBe('service:foo')
    expect(screen.queryByTestId('logs-search-input')).toBeNull()
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([expr]) => expr === 'service:foo')).toBe(true)
  })

  it('advanced mode with empty committed LogsQL queries match-all (*)', async () => {
    renderRoute()
    await screen.findByTestId('logs-search-input')
    vi.mocked(useLogsQuery).mockClear()
    const toggleCheckbox = screen.getByTestId('logs-advanced-toggle').querySelector('input')!
    fireEvent.click(toggleCheckbox)
    await screen.findByTestId('logsql-editor-textarea')
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([expr]) => expr === '*')).toBe(true)
  })

  it('sidebar renders rows from mocked services (desktop)', async () => {
    vi.mocked(useMediaQuery).mockReturnValue(false)
    renderRoute()
    await screen.findByTestId('logs-search-input')
    const rows = screen.getAllByTestId('stream-picker-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveAttribute('data-service', 'home-assistant')
    expect(screen.getByText('1,204')).toBeInTheDocument()
  })

  it('clicking a row selects and writes URL with services param', async () => {
    vi.mocked(useMediaQuery).mockReturnValue(false)
    renderRoute()
    await screen.findByTestId('logs-search-input')
    const rows = screen.getAllByTestId('stream-picker-row')
    fireEvent.click(rows[0]!)
    // Chip should appear
    expect(await screen.findByTestId('service-chip')).toHaveAttribute(
      'data-service',
      'home-assistant',
    )
    // useLogsQuery should be called with the services CSV
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([, , , services]) => services === 'home-assistant')).toBe(true)
  })

  it('chip × removes the service', async () => {
    renderRoute('/logs?services=home-assistant&since=1h')
    await screen.findByTestId('logs-search-input')
    const chip = await screen.findByTestId('service-chip')
    expect(chip).toBeInTheDocument()
    const removeBtn = screen.getByTestId('service-chip-remove')
    fireEvent.click(removeBtn)
    expect(screen.queryByTestId('service-chip')).not.toBeInTheDocument()
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([, , , services]) => services === '')).toBe(true)
  })

  it('services CSV is forwarded into useLogsQuery', async () => {
    renderRoute('/logs?services=a,b')
    await screen.findByTestId('logs-search-input')
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(calls.some(([, , , services]) => services === 'a,b')).toBe(true)
  })

  it('selection survives in advanced mode', async () => {
    renderRoute('/logs?logsql=service%3Afoo&services=nginx')
    const editor = await screen.findByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(editor.value).toBe('service:foo')
    expect(await screen.findByTestId('service-chip')).toHaveAttribute('data-service', 'nginx')
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(
      calls.some(([expr, , , services]) => expr === 'service:foo' && services === 'nginx'),
    ).toBe(true)
  })

  it('selection survives in plain mode', async () => {
    renderRoute('/logs?q=boom&services=nginx')
    const input = await screen.findByTestId<HTMLInputElement>('logs-search-input')
    expect(input.value).toBe('boom')
    expect(await screen.findByTestId('service-chip')).toHaveAttribute('data-service', 'nginx')
    const calls = vi.mocked(useLogsQuery).mock.calls
    expect(
      calls.some(([expr, , , services]) => expr === '_msg:"boom"' && services === 'nginx'),
    ).toBe(true)
  })

  it('shows truncated banner when services are truncated', async () => {
    mockServicesQuery({ data: { services: [{ service: 'a', count: 1 }], truncated: true } })
    vi.mocked(useMediaQuery).mockReturnValue(false)
    renderRoute()
    await screen.findByTestId('logs-search-input')
    expect(screen.getByTestId('stream-picker-truncated')).toBeInTheDocument()
  })

  it('mobile drawer toggle (mobile path)', async () => {
    // Override useMediaQuery per-test to explicitly set mobile mode
    // The default module mock returns true, which means isMobile=true, so we're in mobile path
    vi.mocked(useMediaQuery).mockImplementation((query) => {
      // Return true for the LogsExplorerBody's mobile detection query
      if (query === '(max-width: 767px)') return true
      return true
    })
    renderRoute()
    await screen.findByTestId('logs-search-input')
    // Mobile drawer is hidden by default
    expect(screen.queryByTestId('stream-picker')).not.toBeInTheDocument()
    // Toggle button should be present
    const toggle = screen.getByTestId('stream-picker-toggle')
    expect(toggle).toBeInTheDocument()
    // Click to open — Dialog mounts in a portal, so use findBy (async)
    fireEvent.click(toggle)
    // Assert the dialog is present by checking the StreamPickerSidebar root
    expect(await screen.findByTestId('stream-picker')).toBeInTheDocument()
  })
})
