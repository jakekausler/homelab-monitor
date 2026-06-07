import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LogsExplorerBody } from '../LogsExplorerBody'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { LogLine } from '@/components/logs/types'

/** Assert an indexed access is defined (noUncheckedIndexedAccess). */
function req<T>(value: T | undefined): T {
  if (value === undefined) throw new Error('expected a defined value')
  return value
}

// Mock the API modules
vi.mock('@/api/logs', async (importActual) => {
  const actual = await importActual<typeof import('@/api/logs')>()
  return {
    ...actual,
    useLogsQuery: vi.fn(),
    useLogsServicesQuery: vi.fn(),
    useSurroundingLogs: vi.fn(),
    fetchNewerLogs: vi.fn(),
    fetchOlderLogs: vi.fn(),
  }
})

vi.mock('@/lib/logsTail', () => ({
  useLogsTail: () => ({ status: 'idle', error: null }),
}))

vi.mock('../HistogramChart', () => ({
  HistogramChart: () => null,
}))

vi.mock('../FieldsDiscoveryPanel', () => ({
  FieldsDiscoveryPanel: () => null,
}))

vi.mock('../SavedQueriesPanel', () => ({
  SavedQueriesPanel: () => null,
}))

vi.mock('../QueryHistoryPanel', () => ({
  QueryHistoryPanel: () => null,
}))

vi.mock('../ExportButton', () => ({
  ExportButton: () => null,
}))

import {
  useLogsQuery,
  useLogsServicesQuery,
  useSurroundingLogs,
  fetchNewerLogs,
  fetchOlderLogs,
} from '@/api/logs'

const NORMAL_LINES: LogLine[] = [
  {
    timestamp: '2024-01-15T10:00:00Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Line 1',
    fields: { source_type: 'docker' },
  },
  {
    timestamp: '2024-01-15T10:01:00Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Line 2',
    fields: { source_type: 'docker' },
  },
  {
    timestamp: '2024-01-15T10:02:00Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Line 3',
    fields: { source_type: 'docker' },
  },
]

const SURR_LINES: LogLine[] = [
  {
    timestamp: '2024-01-15T09:55:00Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Before Line 1',
    fields: { source_type: 'docker' },
  },
  ...NORMAL_LINES,
  {
    timestamp: '2024-01-15T10:05:00Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'After Line 3',
    fields: { source_type: 'docker' },
  },
]

const OLDER_LINE: LogLine = {
  timestamp: '2024-01-15T09:50:00Z',
  severity: 'info',
  service: 'nginx',
  host: 'host-1',
  stream: 'stdout',
  message: 'Even older line',
  fields: { source_type: 'docker' },
}

const NEWER_LINE: LogLine = {
  timestamp: '2024-01-15T10:10:00Z',
  severity: 'info',
  service: 'nginx',
  host: 'host-1',
  stream: 'stdout',
  message: 'Even newer line',
  fields: { source_type: 'docker' },
}

describe('LogsExplorerBody (surrounding mode)', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    })

    // Stub DOM methods (jsdom lacks them)
    Element.prototype.scrollIntoView = vi.fn()
    HTMLElement.prototype.scrollTo = vi.fn()

    // Default mocks
    vi.mocked(useLogsQuery).mockReturnValue({
      data: { pages: [{ lines: NORMAL_LINES, next_cursor: null, has_more: false }] },
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useLogsQuery>)

    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: { services: [], truncated: false },
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof useLogsServicesQuery>)

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)
  })

  afterEach(() => {
    cleanup()
  })

  function renderBody() {
    return render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <LogsExplorerBody
            advancedMode={false}
            committedPlainText=""
            livePlainText=""
            committedLogsQl="*"
            liveLogsQl=""
            range={{ kind: 'preset', token: '1h' }}
            selectedIdentities={[]}
            onLivePlainTextChange={() => {}}
            onLiveLogsQlChange={() => {}}
            onToggleAdvanced={() => {}}
            onSubmitSearch={() => {}}
            onClearSearch={() => {}}
            onRangeChange={() => {}}
            onToggleIdentity={() => {}}
            onAddIdentity={() => {}}
            onSelectIdentities={() => {}}
            onDeselectIdentities={() => {}}
            onOpenSave={() => {}}
            onLoadSavedQuery={() => {}}
            onUpdateSavedQuery={() => {}}
            onLoadHistoryEntry={() => {}}
            onAddMsgFilter={() => {}}
            onNarrowRange={() => {}}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
  }

  it('renders normal mode by default', () => {
    renderBody()
    expect(screen.getByTestId('logs-search-input')).toBeInTheDocument()
    expect(screen.getByTestId('logs-refresh')).toBeInTheDocument()
    expect(screen.queryByTestId('surrounding-mode-bar')).not.toBeInTheDocument()
  })

  it('enters surrounding mode and renders surrounding lines', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Click first normal line
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    // Inspector opens
    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })

    // Click show-surrounding-logs
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    // Mode bar appears
    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    // Search input hidden
    expect(screen.queryByTestId('logs-search-input')).not.toBeInTheDocument()

    // Surrounding lines render
    await waitFor(() => {
      expect(screen.getByText('Before Line 1')).toBeInTheDocument()
      expect(screen.getByText('After Line 3')).toBeInTheDocument()
    })
  })

  it('anchor row is highlighted in surrounding mode', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Click first normal line (will become anchor)
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    // Enter mode
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    // Anchor row is selected
    await waitFor(() => {
      const selectedRow = screen.getByTestId('log-row-selected')
      expect(selectedRow).toBeInTheDocument()
      expect(selectedRow).toHaveTextContent('Line 1')
    })
  })

  it('exit restores normal mode and controls', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Enter mode
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    // Exit mode
    await userEvent.click(screen.getByTestId('surrounding-exit'))

    // Mode bar gone
    await waitFor(() => {
      expect(screen.queryByTestId('surrounding-mode-bar')).not.toBeInTheDocument()
    })

    // Normal controls back
    expect(screen.getByTestId('logs-search-input')).toBeInTheDocument()
  })

  it('load-older accumulates lines', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    vi.mocked(fetchOlderLogs).mockResolvedValue([OLDER_LINE])

    renderBody()

    // Enter mode
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByText('Before Line 1')).toBeInTheDocument()
    })

    // Load older
    await userEvent.click(screen.getByTestId('load-older'))

    // Older line appears at top
    await waitFor(() => {
      expect(screen.getByText('Even older line')).toBeInTheDocument()
    })

    // Verify fetchOlderLogs was called with correct params
    expect(vi.mocked(fetchOlderLogs)).toHaveBeenCalled()
  })

  it('load-newer accumulates lines', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    vi.mocked(fetchNewerLogs).mockResolvedValue([NEWER_LINE])

    renderBody()

    // Enter mode
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByText('After Line 3')).toBeInTheDocument()
    })

    // Load newer
    await userEvent.click(screen.getByTestId('load-newer'))

    // Newer line appears at bottom
    await waitFor(() => {
      expect(screen.getByText('Even newer line')).toBeInTheDocument()
    })

    expect(vi.mocked(fetchNewerLogs)).toHaveBeenCalled()
  })

  it('re-anchor swaps to a different line: indicator + window re-seed', async () => {
    // surr1 is anchored on "Line 1"; surr2 (a distinct window) on "Line 3" and
    // includes a marker line absent from surr1, so the re-seed is observable.
    const reanchorMarker: LogLine = {
      timestamp: '2024-01-15T10:03:00Z',
      severity: 'info',
      service: 'nginx',
      host: 'host-1',
      stream: 'stdout',
      message: 'Reanchor marker line',
      fields: { source_type: 'docker' },
    }
    const surr1: LogLine[] = [req(NORMAL_LINES[0]), req(NORMAL_LINES[1])]
    const surr2: LogLine[] = [req(NORMAL_LINES[2]), reanchorMarker]

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: surr1,
        anchor_index: 0,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Enter mode anchored on the FIRST normal line ("Line 1").
    await userEvent.click(req(screen.getAllByTestId('log-row')[0]))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-indicator')).toHaveTextContent('Line 1')
    })
    // surr1 rendered; the re-anchor marker is NOT yet present.
    expect(screen.getByText('Line 1')).toBeInTheDocument()
    expect(screen.queryByText('Reanchor marker line')).not.toBeInTheDocument()

    // Point the window hook at the SECOND anchor's distinct window.
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: surr2,
        anchor_index: 0,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    // In surrounding mode, click a DIFFERENT rendered row (the surr1 "Line 2" row)
    // to open the inspector, then re-trigger "Show surrounding logs" → re-anchor.
    const line2Row = screen
      .getAllByTestId('log-row')
      .find((row) => row.textContent?.includes('Line 2'))
    await userEvent.click(req(line2Row))
    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    // The window re-seeds to surr2: the indicator updates and the new marker
    // line appears (proving resetWindowed ran on re-anchor, not just an indicator swap).
    await waitFor(() => {
      expect(screen.getByTestId('surrounding-indicator')).toHaveTextContent('Line 2')
      expect(screen.getByText('Reanchor marker line')).toBeInTheDocument()
    })
  })

  it('defaults to all-services scope (no service/source_type sent) on enter', async () => {
    // The default scope is now ALL services (the line's `service` is a derived
    // field that may not match a raw VL field). Entering directly must NOT send
    // service/source_type, and never sourceType:'unknown'.
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })

    // Enter directly — default is all-services.
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    const calls = vi.mocked(useSurroundingLogs).mock.calls
    const enabledCalls = calls.filter((c) => c[0]?.enabled === true)
    expect(enabledCalls.length).toBeGreaterThan(0)
    for (const [args] of enabledCalls) {
      expect(args?.service).toBeUndefined()
      expect(args?.sourceType).not.toBe('unknown')
    }
  })

  it('scopes to service+source_type only when toggled to only-this-service AND the line has a raw service field', async () => {
    // A line with BOTH a raw fields.service AND fields.source_type — the only
    // case where the only-this-service clause can actually match in VL.
    const lineWithRawService: LogLine = {
      timestamp: '2024-01-15T10:00:00Z',
      severity: 'info',
      service: 'nginx',
      host: 'host-1',
      stream: 'stdout',
      message: 'Line with raw service',
      fields: { service: 'nginx', source_type: 'docker' },
    }

    vi.mocked(useLogsQuery).mockReturnValue({
      data: { pages: [{ lines: [lineWithRawService], next_cursor: null, has_more: false }] },
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useLogsQuery>)

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: [lineWithRawService],
        anchor_index: 0,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-scope-service')).toBeInTheDocument()
    })

    // Explicitly narrow to only-this-service, then enter.
    await userEvent.click(screen.getByTestId('surrounding-scope-service'))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    const calls = vi.mocked(useSurroundingLogs).mock.calls
    const enabledCalls = calls.filter((c) => c[0]?.enabled === true)
    const matchingCall = enabledCalls.find(
      (c) => c[0]?.service === 'nginx' && c[0]?.sourceType === 'docker',
    )
    expect(matchingCall).toBeDefined()
    const unknownCall = enabledCalls.find((c) => c[0]?.sourceType === 'unknown')
    expect(unknownCall).toBeUndefined()
  })

  it('only-this-service falls back to all-services when the line has no raw service field', async () => {
    // A line whose `service` is DERIVED (no raw fields.service) — toggling to
    // only-this-service must NOT send a dead `service:"x"` clause; fall back to all.
    const derivedServiceLine: LogLine = {
      timestamp: '2024-01-15T10:00:00Z',
      severity: 'info',
      service: 'sshd',
      host: 'host-1',
      stream: 'stdout',
      message: 'Derived-service line',
      fields: { source_type: 'journald' }, // no raw `service` field
    }

    vi.mocked(useLogsQuery).mockReturnValue({
      data: { pages: [{ lines: [derivedServiceLine], next_cursor: null, has_more: false }] },
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useLogsQuery>)

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: [derivedServiceLine],
        anchor_index: 0,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-scope-service')).toBeInTheDocument()
    })

    await userEvent.click(screen.getByTestId('surrounding-scope-service'))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    const calls = vi.mocked(useSurroundingLogs).mock.calls
    const enabledCalls = calls.filter((c) => c[0]?.enabled === true)
    expect(enabledCalls.length).toBeGreaterThan(0)
    for (const [args] of enabledCalls) {
      // No raw service field → fall back to all-services (no clause).
      expect(args?.service).toBeUndefined()
      expect(args?.sourceType).not.toBe('unknown')
    }
  })

  it('falls back to all-services (no source_type) when anchor line has no source_type', async () => {
    const lineWithoutSourceType: LogLine = {
      timestamp: '2024-01-15T10:00:00Z',
      severity: 'info',
      service: 'nginx',
      host: 'host-1',
      stream: 'stdout',
      message: 'Line without source_type',
      fields: {},
    }

    vi.mocked(useLogsQuery).mockReturnValue({
      data: { pages: [{ lines: [lineWithoutSourceType], next_cursor: null, has_more: false }] },
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useLogsQuery>)

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: [lineWithoutSourceType],
        anchor_index: 0,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })

    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    const calls = vi.mocked(useSurroundingLogs).mock.calls
    const enabledCalls = calls.filter((c) => c[0]?.enabled === true)
    expect(enabledCalls.length).toBeGreaterThan(0)

    // All enabled calls must omit service and sourceType (all-services fallback)
    for (const [args] of enabledCalls) {
      expect(args?.service).toBeUndefined()
      // CRUCIAL: sourceType must never be 'unknown'
      expect(args?.sourceType).not.toBe('unknown')
    }
  })

  it('scopeAll=true sends no service/sourceType even when source_type is present', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })

    // Switch to All-services scope before entering (testid from FieldInspectorPanel)
    await userEvent.click(screen.getByTestId('surrounding-scope-all'))

    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    const calls = vi.mocked(useSurroundingLogs).mock.calls
    const enabledCalls = calls.filter((c) => c[0]?.enabled === true)
    expect(enabledCalls.length).toBeGreaterThan(0)

    for (const [args] of enabledCalls) {
      expect(args?.service).toBeUndefined()
      expect(args?.sourceType).toBeUndefined()
    }
  })

  it('highlights the anchor via backend anchor_index even when the clicked line diverges from the window copy', async () => {
    // Regression: in all-services scope, the normal-query line's stream/timestamp
    // can differ from the window copy's stream/timestamp.  Old code did an exact
    // triple-find (timestamp|stream|message) against the NORMAL line, which
    // returned -1 when the window line had a divergent stream or higher-precision
    // timestamp → no highlight.  The fix captures anchor identity from
    // data.lines[anchor_index] (the backend-authoritative copy) into
    // surrAnchorKey and builds the row key from the WINDOWED line's own fields.

    // Normal query line — stream and timestamp differ from the window's anchor copy.
    const normalAnchorLine: LogLine = {
      timestamp: '2024-01-15T10:00:00.000Z',
      stream: 'normal-stream-id',
      service: 'nginx',
      host: 'host-1',
      message: 'Anchor line',
      severity: 'info',
      fields: { source_type: 'docker' },
    }

    vi.mocked(useLogsQuery).mockReturnValue({
      data: { pages: [{ lines: [normalAnchorLine], next_cursor: null, has_more: false }] },
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useLogsQuery>)

    // Window lines — anchor (index 1) has DIFFERENT stream + more-precise timestamp.
    const windowBeforeLine: LogLine = {
      timestamp: '2024-01-15T09:59:59.000Z',
      stream: 'window-stream-id',
      service: 'nginx',
      host: 'host-1',
      message: 'Before anchor',
      severity: 'info',
      fields: { source_type: 'docker' },
    }
    const windowAnchorLine: LogLine = {
      timestamp: '2024-01-15T10:00:00.123456789Z', // more-precise — diverges from normal
      stream: 'window-stream-id', // different stream — diverges from normal
      service: 'nginx',
      host: 'host-1',
      message: 'Anchor line',
      severity: 'info',
      fields: { source_type: 'docker' },
    }
    const windowAfterLine: LogLine = {
      timestamp: '2024-01-15T10:00:01.000Z',
      stream: 'window-stream-id',
      service: 'nginx',
      host: 'host-1',
      message: 'After anchor',
      severity: 'info',
      fields: { source_type: 'docker' },
    }

    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: [windowBeforeLine, windowAnchorLine, windowAfterLine],
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Click the normal line to open the inspector.
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))

    await waitFor(() => {
      expect(screen.getByTestId('show-surrounding-logs')).toBeInTheDocument()
    })

    // Enter surrounding mode (default all-services scope).
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    // Exactly one highlighted row must exist, and it must be the window's anchor copy.
    await waitFor(() => {
      const selectedRow = screen.getByTestId('log-row-selected')
      expect(selectedRow).toBeInTheDocument()
      expect(selectedRow).toHaveTextContent('Anchor line')
    })
  })

  it('controls are hidden in surrounding mode', async () => {
    vi.mocked(useSurroundingLogs).mockReturnValue({
      data: {
        lines: SURR_LINES,
        anchor_index: 1,
        truncated_before: false,
        truncated_after: false,
        degraded: false,
      },
      isLoading: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useSurroundingLogs>)

    renderBody()

    // Enter mode
    const logRows = screen.getAllByTestId('log-row')
    await userEvent.click(req(logRows[0]))
    await userEvent.click(screen.getByTestId('show-surrounding-logs'))

    await waitFor(() => {
      expect(screen.getByTestId('surrounding-mode-bar')).toBeInTheDocument()
    })

    // Controls are hidden
    expect(screen.queryByTestId('logs-search-input')).not.toBeInTheDocument()
    expect(screen.queryByTestId('logs-refresh')).not.toBeInTheDocument()
    expect(screen.queryByTestId('logs-tail-toggle')).not.toBeInTheDocument()
    expect(screen.queryByTestId('logs-filter-toggle')).not.toBeInTheDocument()
    expect(screen.queryByTestId('logs-save-query')).not.toBeInTheDocument()
  })
})
