import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { DockerContainerLogsViewerBody } from '@/routes/integrations/DockerContainerLogsViewerBody'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(cleanup)
afterEach(() => {
  localStorage.removeItem('homelab-monitor:timezone')
})

vi.mock('@/api/docker', () => ({
  useContainerLogs: vi.fn(),
  useListContainers: vi.fn(),
  dockerLogsQueryKeys: {
    logs: (n: string, range: { since: string } | { start: string; end: string }) => [
      'integrations',
      'docker',
      'containers',
      n,
      'logs',
      'since' in range ? `since:${range.since}` : `range:${range.start}..${range.end}`,
    ],
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
          <DockerContainerLogsViewerBody containerName={NAME} since="15m" onRangeChange={vi.fn()} />
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
      data: { pages: [makeData()], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)
  })

  it('renders available state with lines + timestamps', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeData({
            log_status: 'available',
            lines: [
              { timestamp: '2026-05-21T14:30:00Z', message: 'INFO line 1' },
              { timestamp: '2026-05-21T14:30:05Z', message: 'INFO line 2' },
            ],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)
    renderBody()
    const body = await screen.findByTestId('logs-body')
    expect(body.textContent).toContain('INFO line 1')
    expect(body.textContent).toContain('INFO line 2')
    // Default render is local (America/New_York, EDT in May, UTC-4).
    expect(body.textContent).toContain('2026-05-21 10:30:00 EDT')
    const lastLogAt = screen.getByTestId('last-log-at')
    expect(lastLogAt.textContent).toContain('Last: 2026-05-21 10:30:05 EDT')
  })

  it('renders no_lines empty state', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeData({ log_status: 'no_lines', lines: [] })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
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
      data: { pages: [], pageParams: [] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
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
      data: { pages: [], pageParams: [] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)
    renderBody()
    expect(await screen.findByTestId('unavailable-banner')).toBeInTheDocument()
  })

  it('renders truncated banner when truncated=true', async () => {
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: { pages: [makeData({ truncated: true })], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
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

  it('selecting a preset in the time-range control updates the URL via onRangeChange', async () => {
    const onRangeChange = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <DockerContainerLogsViewerBody
            containerName={NAME}
            since="15m"
            onRangeChange={onRangeChange}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByTestId('time-range-trigger'))
    fireEvent.click(screen.getByTestId('preset-1h'))
    expect(onRangeChange).toHaveBeenCalledWith({
      since: '1h',
      start: undefined,
      end: undefined,
    })
  })

  it('passes the preset since to useContainerLogs as a range object', async () => {
    renderBody()
    await screen.findByTestId('logs-body')
    const calls = vi.mocked(useContainerLogs).mock.calls
    expect(calls.some(([, range]) => 'since' in (range as object))).toBe(true)
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
      data: { pages: [makeData()], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)
    renderBody()
    const btn = await screen.findByTestId('refresh-logs')
    expect(btn).toBeDisabled()
  })

  it('renders the wrap toggle', async () => {
    renderBody()
    expect(await screen.findByTestId('wrap-toggle')).toBeInTheDocument()
  })

  it('toggling wrap switches the log body to wrapping mode', async () => {
    renderBody()
    await screen.findByTestId('logs-body')
    const toggle = screen.getByTestId('wrap-toggle')
    const checkbox = within(toggle).getByRole('checkbox')
    expect(checkbox).not.toBeChecked()
    fireEvent.click(checkbox)
    expect(checkbox).toBeChecked()
    expect(screen.getByTestId('logs-body').className).toContain('whitespace-normal')
  })

  it('clicking the UTC timezone toggle flips both row and header timestamps', async () => {
    // Ensure localStorage is clean so the hook initialises to 'local' (default).
    localStorage.removeItem('homelab-monitor:timezone')

    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-21T14:30:00Z', message: 'toggle-test-line' }],
          }),
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)

    renderBody()
    const body = await screen.findByTestId('logs-body')
    const lastLogAt = screen.getByTestId('last-log-at')

    // Default (local / EDT, UTC-4 in May): row and header both show EDT.
    expect(body.textContent).toContain('2026-05-21 10:30:00 EDT')
    expect(lastLogAt.textContent).toContain('2026-05-21 10:30:00 EDT')

    // Click the UTC toggle (the checkbox inside data-testid="timezone-toggle").
    const toggleLabel = screen.getByTestId('timezone-toggle')
    const checkbox = toggleLabel.querySelector('input[type="checkbox"]')!
    expect(checkbox).not.toBeChecked()
    fireEvent.click(checkbox)
    expect(checkbox).toBeChecked()

    // After toggle: BOTH row timestamp AND the "Last:" header timestamp flip to UTC.
    expect(body.textContent).toContain('2026-05-21 14:30:00 UTC')
    expect(screen.getByTestId('last-log-at').textContent).toContain('2026-05-21 14:30:00 UTC')
  })

  it('renders older pages above newer pages in multi-page load', async () => {
    // pages[0] = newest window (newer-1), pages[1] = older window (older-1)
    // After reverse, should render: older-1 then newer-1
    vi.mocked(useContainerLogs).mockReturnValue({
      isLoading: false,
      isFetching: false,
      error: null,
      data: {
        pages: [
          makeData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-21T14:30:10Z', message: 'newer-1' }],
          }),
          makeData({
            log_status: 'available',
            lines: [{ timestamp: '2026-05-21T14:30:00Z', message: 'older-1' }],
          }),
        ],
        pageParams: [undefined, 'cursor-1'],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    } as never)
    renderBody()
    const body = await screen.findByTestId('logs-body')
    const text = body.textContent ?? ''
    expect(text.indexOf('older-1')).toBeLessThan(text.indexOf('newer-1'))
  })

  it('Refresh on open-end window re-resolves end to a later "now"', async () => {
    const startIso = '2026-05-21T14:00:00.000Z'
    const t0 = new Date('2026-05-21T15:00:00.000Z')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(t0)

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <DockerContainerLogsViewerBody
            containerName={NAME}
            start={startIso}
            onRangeChange={vi.fn()}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
    await screen.findByTestId('logs-body')

    // Capture the end from the first batch of calls (open-end resolved to t0).
    const callsBefore = vi.mocked(useContainerLogs).mock.calls
    const firstCustomCall = callsBefore.find(
      ([, range]) => 'start' in (range as object) && 'end' in (range as object),
    )
    expect(firstCustomCall).toBeDefined()
    const end1 = (firstCustomCall![1] as { start: string; end: string }).end

    // Advance time by 60 seconds and click Refresh → refreshNonce bumps →
    // useMemo re-runs with new Date() = t0 + 60s → end re-resolves to a later timestamp.
    vi.setSystemTime(new Date(t0.getTime() + 60_000))
    fireEvent.click(screen.getByTestId('refresh-logs'))

    // After the click, useContainerLogs should have been called again with a later end.
    const callsAfter = vi.mocked(useContainerLogs).mock.calls
    const laterCalls = callsAfter.filter(
      ([, range]) => 'start' in (range as object) && 'end' in (range as object),
    )
    const end2 = (laterCalls[laterCalls.length - 1]![1] as { start: string; end: string }).end
    expect(new Date(end2).getTime()).toBeGreaterThan(new Date(end1).getTime())

    vi.useRealTimers()
  })

  it('open-bound custom: start-only URL activates custom mode and passes resolved {start,end} to useContainerLogs', async () => {
    const startIso = '2026-05-21T14:00:00.000Z'
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <DockerContainerLogsViewerBody
            containerName={NAME}
            start={startIso}
            onRangeChange={vi.fn()}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
    await screen.findByTestId('logs-body')
    // useContainerLogs must have been called with a {start, end} range (not {since}).
    const calls = vi.mocked(useContainerLogs).mock.calls
    const customCall = calls.find(
      ([, range]) => 'start' in (range as object) && 'end' in (range as object),
    )
    expect(customCall).toBeDefined()
    // end should be a recent ISO string (resolved to ~now), not undefined or empty.
    const range = customCall![1] as { start: string; end: string }
    expect(range.start).toBe(startIso)
    expect(typeof range.end).toBe('string')
    expect(range.end.length).toBeGreaterThan(0)
  })
})
