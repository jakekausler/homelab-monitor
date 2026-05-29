import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { DockerContainerLogsViewerBody } from '@/routes/integrations/DockerContainerLogsViewerBody'
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
    lines: Array<{ timestamp: string; message: string }>
    truncated: boolean
  }> = {},
) {
  return {
    container_name: NAME,
    log_status: 'available' as const,
    lines: [{ timestamp: '2026-05-21T14:30:00Z', message: 'INFO hello' }],
    truncated: false,
    window_start: '2026-05-21T14:15:00Z',
    window_end: '2026-05-21T14:30:00Z',
    ...overrides,
  }
}

function renderBody() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <DockerContainerLogsViewerBody containerName={NAME} />
        </TooltipProvider>
      </QueryClientProvider>,
    ),
  }
}

describe('DockerContainerLogsViewerBody', () => {
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
          { timestamp: '2026-05-21T14:30:00Z', message: 'INFO line 1' },
          { timestamp: '2026-05-21T14:30:05Z', message: 'INFO line 2' },
        ],
      }),
    } as never)
    renderBody()
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
    renderBody()
    expect(await screen.findByTestId('no-lines')).toBeInTheDocument()
    expect(screen.getByTestId('no-lines')).toHaveTextContent('Try widening')
  })

  it('renders container_unknown 404 state', async () => {
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
    renderBody()
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
    renderBody()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })

  it('renders truncated banner when truncated=true', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: makeData({ truncated: true }),
    } as never)
    renderBody()
    expect(await screen.findByTestId('truncated-banner')).toBeInTheDocument()
    expect(screen.getByTestId('truncated-banner')).toHaveTextContent('Narrow the time window')
  })

  it('omits truncated banner when truncated=false', async () => {
    renderBody()
    await screen.findByTestId('logs-body')
    expect(screen.queryByTestId('truncated-banner')).toBeNull()
  })

  it('since picker change triggers new query', async () => {
    renderBody()
    const picker = await screen.findByTestId<HTMLSelectElement>('since-picker')
    fireEvent.change(picker, { target: { value: '1h' } })
    // Assert the hook was called twice: once with default '15m', once with '1h'.
    const calls = vi.mocked(useContainerLogs).mock.calls
    expect(calls.some(([, since]) => since === '1h')).toBe(true)
  })

  it('Refresh button calls invalidateQueries', async () => {
    const { qc } = renderBody()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const btn = await screen.findByTestId('refresh-logs')
    fireEvent.click(btn)
    expect(invalidateSpy).toHaveBeenCalled()
  })

  it('renders status badge from useListContainers cache', () => {
    renderBody()
    // Container name should be rendered
    expect(screen.getByText(NAME)).toBeInTheDocument()
    // StatusBadge will render the 'Running' status text
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('falls back to name-only if container not in list cache', () => {
    vi.mocked(useListContainers).mockReturnValue({ data: { containers: [] } } as never)
    renderBody()
    expect(screen.getByText(NAME)).toBeInTheDocument()
    // No StatusBadge rendered → no 'Running' status text
    expect(screen.queryByText('Running')).not.toBeInTheDocument()
  })

  it('Refresh button is disabled while isFetching=true', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: true,
      error: null,
      data: makeData(),
    } as never)
    renderBody()
    const btn = await screen.findByTestId('refresh-logs')
    expect(btn).toBeDisabled()
  })
})
