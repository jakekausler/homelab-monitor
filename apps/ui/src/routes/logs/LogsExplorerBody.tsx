import { useMemo, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'

import { ApiError } from '@/api/client'
import { useLogsQuery } from '@/api/logs'
import { Button } from '@/components/ui/button'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { translateSearchToLogsQl } from '@/lib/logsQlTranslate'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import {
  ALL_PRESETS,
  resolveCustomWindow,
  resolvePreset,
  toIsoZ,
  type TimeRangeValue,
} from '@/lib/timeRange'
import type { LogViewerStatus, UseLogsResult } from '@/components/logs/types'

const EMPTY_COPY = 'No matches in the selected range. Try a wider time range or a different query.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

interface LogsExplorerBodyProps {
  /** The COMMITTED search text (already reflected in the URL ?q). */
  committedSearchText: string
  /** The live (uncommitted) text shown in the input. */
  liveSearchText: string
  /** Committed time range (mirrors the URL). */
  range: TimeRangeValue
  /** Update the live input text (no query/URL change). */
  onLiveSearchTextChange: (next: string) => void
  /** Commit the live text → updates URL ?q and triggers the query. */
  onSubmitSearch: () => void
  /** Clear the search text (commits empty → omits ?q). */
  onClearSearch: () => void
  /** Range picker change → Page writes URL (since OR start/end). */
  onRangeChange: (next: TimeRangeValue) => void
}

export function LogsExplorerBody({
  committedSearchText,
  liveSearchText,
  range,
  onLiveSearchTextChange,
  onSubmitSearch,
  onClearSearch,
  onRangeChange,
}: LogsExplorerBodyProps) {
  const [wrap, setWrap] = useState(false)
  // STAGE-004-009 timezone wiring (mirrors the Docker viewer).
  const [timezone, toggleTimezone] = useTimezonePreference()
  // Bumping this re-resolves the window against a fresh "now" (Refresh / live-tail
  // groundwork) WITHOUT churning the query key on every render.
  const [refreshNonce, setRefreshNonce] = useState(0)

  const expr = translateSearchToLogsQl(committedSearchText)

  // Resolve the committed range to absolute [startIso, endIso]. `now` must stay
  // STABLE across renders (else the open-end window re-reads new Date() each
  // render → query-key churn → refetch loop). Memoize on the committed range +
  // refreshNonce so `now` only advances when the user changes the range or hits
  // Refresh. Mirrors DockerContainerLogsViewerBody's useMemo pattern.
  const { startIso, endIso } = useMemo(() => {
    const now = new Date()
    const win =
      range.kind === 'preset'
        ? resolvePreset(range.token, now)
        : resolveCustomWindow({ start: range.start, end: range.end }, { now, maxSpanDays: 30 })
    return { startIso: toIsoZ(win.start), endIso: toIsoZ(win.end) }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: re-resolve only on committed range change or explicit refresh
  }, [
    range.kind,
    range.kind === 'preset' ? range.token : undefined,
    range.kind === 'custom' ? range.start?.getTime() : undefined,
    range.kind === 'custom' ? range.end?.getTime() : undefined,
    refreshNonce,
  ])

  // The query is ALWAYS enabled here: expr is never empty (an empty search box
  // resolves to '*'), and startIso/endIso are always non-empty ISO strings. Do
  // NOT add a redundant empty-guard — useLogsQuery's `enabled` is effectively
  // always true for this consumer by design.
  const logs = useLogsQuery(expr, startIso, endIso)

  const handleRefresh = (): void => {
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  // Backend surfaces VictoriaLogs unavailability as HTTP 502 upstream_unavailable
  // (see apps/monitor/.../api/routers/logs.py). There is NO 503/404 on this
  // endpoint. Everything else non-2xx is a generic error.
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  const pages = logs.data?.pages ?? []
  // pages[0] is the NEWEST window; reverse so oldest renders first (mirrors the
  // Docker viewer). LogsQueryResponse has NO log_status/truncated fields, so we
  // derive logStatus below from line presence.
  const flatLines = pages
    .slice()
    .reverse()
    .flatMap((p) => p.lines)
  const hasData = logs.data !== undefined

  const header = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* STAGE-004-011 will add an advanced LogsQL (CodeMirror) editor here; keep this input swappable. */}
        <form
          className="flex flex-1 items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            onSubmitSearch()
          }}
        >
          <input
            type="text"
            data-testid="logs-search-input"
            aria-label="Search logs"
            className="flex h-9 w-full max-w-md rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            placeholder="Search logs (plain text)…"
            value={liveSearchText}
            onChange={(e) => {
              onLiveSearchTextChange(e.target.value)
            }}
          />
          {(liveSearchText.length > 0 || committedSearchText.length > 0) && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid="logs-search-clear"
              aria-label="Clear search"
              onClick={onClearSearch}
            >
              <X className="size-4" />
            </Button>
          )}
          <Button type="submit" size="sm" data-testid="logs-search-submit">
            Search
          </Button>
        </form>
        <div className="flex items-center gap-2">
          <WrapToggle checked={wrap} onChange={setWrap} id="logs-wrap" />
          <TimezoneToggle
            checked={timezone === 'utc'}
            onChange={toggleTimezone}
            id="logs-tz-toggle"
          />
          <TimeRangeControl
            mode="full"
            value={range}
            onChange={onRangeChange}
            presets={ALL_PRESETS}
          />
          <Button
            size="sm"
            variant="outline"
            onClick={handleRefresh}
            disabled={logs.isFetching}
            data-testid="logs-refresh"
          >
            <RefreshCw className="mr-1 size-4" />
            {logs.isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </div>
      {isGenericApiError && (
        <p role="alert" className="text-sm text-red-600">
          Failed to load logs: {logs.error?.message}
        </p>
      )}
    </>
  )

  const useLogs = (): UseLogsResult => {
    if (isUnavailable) {
      return {
        lines: undefined,
        isLoading: false,
        isError: true,
        error: logs.error instanceof ApiError ? logs.error : undefined,
        logStatus: 'unavailable',
      }
    }
    if (isGenericApiError) {
      return {
        lines: undefined,
        isLoading: false,
        isError: false,
        error: undefined,
      }
    }
    // LogsQueryResponse carries NO log_status — derive it: data present with
    // zero lines → 'no_lines'; data present with lines → 'available'; no data
    // yet (still loading / not enabled) → undefined (LogViewer shows nothing
    // until isLoading or a status resolves).
    const status: LogViewerStatus | undefined = !hasData
      ? undefined
      : flatLines.length === 0
        ? 'no_lines'
        : 'available'
    return {
      lines: flatLines,
      isLoading: logs.isLoading,
      isError: false,
      error: undefined,
      logStatus: status,
      // LogsQueryResponse has no `truncated` field — pagination (has_more →
      // hasNextPage) is the only "more results" signal. Do NOT set truncated.
      hasMore: logs.hasNextPage,
      isLoadingOlder: logs.isFetchingNextPage,
      loadOlder: () => {
        void logs.fetchNextPage()
      },
    }
  }

  return (
    <LogViewer
      useLogs={useLogs}
      headerSlot={header}
      emptyStateCopy={EMPTY_COPY}
      unavailableCopy={UNAVAILABLE_COPY}
      wrap={wrap}
      timezone={timezone}
    />
  )
}
