import { useMemo, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'

import { ApiError } from '@/api/client'
import { useLogsQuery, useLogsServicesQuery } from '@/api/logs'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { AdvancedToggle } from '@/components/logs/AdvancedToggle'
import { LogViewer } from '@/components/logs/LogViewer'
import { LogsQlEditor } from '@/components/logs/LogsQlEditor'
import { StreamPickerSidebar } from '@/components/logs/StreamPickerSidebar'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { translateSearchToLogsQl } from '@/lib/logsQlTranslate'
import { useMediaQuery } from '@/lib/useMediaQuery'
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
  /** Advanced (raw LogsQL) mode vs plain-text mode. */
  advancedMode: boolean
  /** COMMITTED plain-text search (reflected in the URL ?q when plain mode). */
  committedPlainText: string
  /** Live (uncommitted) plain-text input value. */
  livePlainText: string
  /** COMMITTED raw LogsQL (reflected in the URL ?logsql when advanced mode). */
  committedLogsQl: string
  /** Live (uncommitted) LogsQL editor value. */
  liveLogsQl: string
  /** Committed time range (mirrors the URL). */
  range: TimeRangeValue
  /** Selected service values (AND'd server-side). */
  selectedServices: string[]
  /** Update the live plain-text input (no query/URL change). */
  onLivePlainTextChange: (next: string) => void
  /** Update the live LogsQL editor text (no query/URL change). */
  onLiveLogsQlChange: (next: string) => void
  /** Flip advanced/plain mode (preserves both texts; rewrites the URL). */
  onToggleAdvanced: (next: boolean) => void
  /** Commit the active mode's live text → updates URL + triggers the query. */
  onSubmitSearch: () => void
  /** Clear the ACTIVE mode's text (commits empty → omits that URL key). */
  onClearSearch: () => void
  /** Range picker change → Page writes URL (since OR start/end). */
  onRangeChange: (next: TimeRangeValue) => void
  /** Toggle a service in/out of the selection (row click + chip ×). */
  onToggleService: (service: string) => void
}

export function LogsExplorerBody({
  advancedMode,
  committedPlainText,
  livePlainText,
  committedLogsQl,
  liveLogsQl,
  range,
  selectedServices,
  onLivePlainTextChange,
  onLiveLogsQlChange,
  onToggleAdvanced,
  onSubmitSearch,
  onClearSearch,
  onRangeChange,
  onToggleService,
}: LogsExplorerBodyProps) {
  const [wrap, setWrap] = useState(false)
  // STAGE-004-009 timezone wiring (mirrors the Docker viewer).
  const [timezone, toggleTimezone] = useTimezonePreference()
  // Bumping this re-resolves the window against a fresh "now" (Refresh / live-tail
  // groundwork) WITHOUT churning the query key on every render.
  const [refreshNonce, setRefreshNonce] = useState(0)

  // Active mode decides the expr: advanced sends the COMMITTED raw LogsQL
  // verbatim (empty → match-all '*' to keep the always-enabled invariant);
  // plain translates the committed text into _msg:"…".
  const expr = advancedMode
    ? committedLogsQl.trim().length > 0
      ? committedLogsQl
      : '*'
    : translateSearchToLogsQl(committedPlainText)

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
  const servicesCsv = selectedServices.join(',')
  const logs = useLogsQuery(expr, startIso, endIso, servicesCsv)

  // Services query — depends on window ONLY, window-only refetch.
  const servicesQuery = useLogsServicesQuery(startIso, endIso)
  const servicesData = servicesQuery.data

  // Mobile detection + drawer state.
  const isMobile = useMediaQuery('(max-width: 767px)')
  const [pickerOpen, setPickerOpen] = useState(false)

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
      {selectedServices.length > 0 && (
        <div data-testid="selected-services" className="flex flex-wrap items-center gap-2">
          {selectedServices.map((svc) => (
            <span
              key={svc}
              data-testid="service-chip"
              data-service={svc}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-accent px-2 py-0.5 text-xs"
            >
              {svc}
              <button
                type="button"
                aria-label={`Remove ${svc}`}
                data-testid="service-chip-remove"
                onClick={() => onToggleService(svc)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <form
          className="flex flex-1 items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            onSubmitSearch()
          }}
        >
          {advancedMode ? (
            <LogsQlEditor
              value={liveLogsQl}
              onChange={onLiveLogsQlChange}
              onSubmit={onSubmitSearch}
              placeholder="Enter LogsQL…"
              ariaLabel="LogsQL query"
              className="w-full max-w-md"
            />
          ) : (
            <input
              type="text"
              data-testid="logs-search-input"
              aria-label="Search logs"
              className="flex h-9 w-full max-w-md rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="Search logs (plain text)…"
              value={livePlainText}
              onChange={(e) => {
                onLivePlainTextChange(e.target.value)
              }}
            />
          )}
          {(advancedMode
            ? liveLogsQl.length > 0 || committedLogsQl.length > 0
            : livePlainText.length > 0 || committedPlainText.length > 0) && (
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
          <AdvancedToggle checked={advancedMode} onChange={onToggleAdvanced} id="logs-advanced" />
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

  const sidebar = (
    <StreamPickerSidebar
      services={servicesData?.services ?? []}
      truncated={servicesData?.truncated ?? false}
      selectedServices={selectedServices}
      onToggleService={onToggleService}
      isLoading={servicesQuery.isLoading}
      isError={servicesQuery.isError}
    />
  )

  return (
    <div className="flex gap-4">
      {!isMobile && <aside className="hidden w-56 shrink-0 md:block">{sidebar}</aside>}
      <div className="min-w-0 flex-1">
        {isMobile && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid="stream-picker-toggle"
            onClick={() => setPickerOpen(true)}
            className="mb-2"
          >
            Services
          </Button>
        )}
        <LogViewer
          useLogs={useLogs}
          headerSlot={header}
          emptyStateCopy={EMPTY_COPY}
          unavailableCopy={UNAVAILABLE_COPY}
          wrap={wrap}
          timezone={timezone}
        />
      </div>
      {isMobile && (
        <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
          <DialogContent className="max-w-xs">
            <DialogTitle>Services</DialogTitle>
            {sidebar}
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
