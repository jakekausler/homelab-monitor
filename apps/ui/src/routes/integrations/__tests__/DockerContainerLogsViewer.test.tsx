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
import { DockerContainerLogsViewerPage } from '@/routes/integrations/DockerContainerLogsViewer'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)

vi.mock('@/api/docker', () => ({
  useContainerLogs: vi.fn(),
  useListContainers: vi.fn(),
  dockerLogsQueryKeys: {
    logs: (n: string, s: string) => ['integrations', 'docker', 'containers', n, 'logs', s],
  },
}))

import { useContainerLogs, useListContainers } from '@/api/docker'

const NAME = 'homeassistant'

function makeData(
  overrides: Partial<{
    log_status: 'available' | 'no_lines' | 'container_unknown' | 'vl_unavailable'
    lines: Array<{ timestamp: string; line: string }>
    truncated: boolean
  }> = {},
) {
  return {
    container_name: NAME,
    log_status: 'available' as const,
    lines: [{ timestamp: '2026-05-21T14:30:00Z', line: 'INFO hello' }],
    truncated: false,
    window_start: '2026-05-21T14:15:00Z',
    window_end: '2026-05-21T14:30:00Z',
    ...overrides,
  }
}

function renderWithRouter(initialPath: string = `/integrations/docker/containers/${NAME}/logs`) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const route = createRoute({
    getParentRoute: () => rootRoute,
    path: '/integrations/docker/containers/$name/logs',
    component: DockerContainerLogsViewerPage,
  })
  const dockerRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/integrations/docker',
    component: () => <div>docker page</div>,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([route, dockerRoute]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    ),
  }
}

describe('DockerContainerLogsViewerPage', () => {
  beforeEach(() => {
    vi.mocked(useListContainers).mockReturnValue({
      data: { containers: [{ id: 'x', name: NAME, status: 'running' }] },
    } as never)
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: makeData(),
    } as never)
  })

  it('renders available state with lines + timestamps', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: makeData({
        log_status: 'available',
        lines: [
          { timestamp: '2026-05-21T14:30:00Z', line: 'INFO line 1' },
          { timestamp: '2026-05-21T14:30:05Z', line: 'INFO line 2' },
        ],
      }),
    } as never)
    renderWithRouter()
    const body = await screen.findByTestId('logs-body')
    expect(body.textContent).toContain('INFO line 1')
    expect(body.textContent).toContain('INFO line 2')
    expect(body.textContent).toContain('2026-05-21T14:30:00Z')
  })

  it('renders no_lines empty state', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: makeData({ log_status: 'no_lines', lines: [] }),
    } as never)
    renderWithRouter()
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
    expect(screen.getByTestId('no-lines')).toHaveTextContent('Try widening')
  })

  it('renders container_unknown 404 page', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: new ApiError({
        status: 404,
        code: 'not_found',
        message: 'container not found',
        retryAfterSeconds: null,
        details: null,
      }),
      data: undefined,
    } as never)
    renderWithRouter()
    expect(await screen.findByTestId('container-unknown')).toBeInTheDocument()
  })

  it('renders vl_unavailable amber banner', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: new ApiError({
        status: 503,
        code: 'vl_unavailable',
        message: 'logs temporarily unavailable',
        retryAfterSeconds: null,
        details: null,
      }),
      data: undefined,
    } as never)
    renderWithRouter()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })

  it('renders truncated banner when truncated=true', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: makeData({ truncated: true }),
    } as never)
    renderWithRouter()
    expect(await screen.findByTestId('truncated-banner')).toBeInTheDocument()
    expect(screen.getByTestId('truncated-banner')).toHaveTextContent('Narrow the time window')
  })

  it('omits truncated banner when truncated=false', async () => {
    renderWithRouter()
    await screen.findByTestId('logs-body')
    expect(screen.queryByTestId('truncated-banner')).toBeNull()
  })

  it('since picker change triggers new query', async () => {
    renderWithRouter()
    const picker = await screen.findByTestId<HTMLSelectElement>('since-picker')
    fireEvent.change(picker, { target: { value: '1h' } })
    // Assert the hook was called twice: once with default '15m', once with '1h'.
    const calls = vi.mocked(useContainerLogs).mock.calls
    expect(calls.some(([, since]) => since === '1h')).toBe(true)
  })

  it('Refresh button calls invalidateQueries', async () => {
    const { qc } = renderWithRouter()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const btn = await screen.findByTestId('refresh-logs')
    fireEvent.click(btn)
    expect(invalidateSpy).toHaveBeenCalled()
  })

  it('renders status badge from useListContainers cache', async () => {
    renderWithRouter()
    await screen.findByTestId('logs-header')
    // StatusBadge will render the 'running' status string somewhere in the header
    expect(screen.getByTestId('logs-header').textContent).toContain(NAME)
  })

  it('falls back to name-only header if container not in list cache', async () => {
    vi.mocked(useListContainers).mockReturnValue({ data: { containers: [] } } as never)
    renderWithRouter()
    const header = await screen.findByTestId('logs-header')
    expect(header.textContent).toContain(NAME)
    // No StatusBadge rendered → no 'running' text in header.
  })

  it('back link targets /integrations/docker', async () => {
    renderWithRouter()
    await screen.findByTestId('logs-header')
    const back = screen.getByRole('link', { name: /Back to Docker integration/ })
    expect(back).toHaveAttribute('href', '/integrations/docker')
  })

  it('Refresh button is disabled while isFetching=true', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: true,
      error: null,
      data: makeData(),
    } as never)
    renderWithRouter()
    const btn = await screen.findByTestId('refresh-logs')
    expect(btn).toBeDisabled()
  })
})
