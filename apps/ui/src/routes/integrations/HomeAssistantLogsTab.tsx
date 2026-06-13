// STAGE-005-025 — HA Logs tab: the shared <LogViewer> scoped to the
// `docker:homeassistant` stream. Mirrors DockerContainerLogsViewerBody's
// header + window-memo + refreshNonce shape, but reads the GENERIC
// useLogsQuery (LogsExplorerBody's data source) since HA logs are not a
// docker-API container-logs concern here — they are a VictoriaLogs stream.
//
// Default view: errors-focused (severity:error OR severity:warn) over 1h.
// An "Errors only ↔ All lines" toggle swaps expr between that and match-all '*'.
import { useMemo, useState, type JSX } from 'react'
import { RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { identitiesToServicesCsv, useLogsQuery } from '@/api/logs'
import { Button } from '@/components/ui/button'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import {
  ALL_PRESETS,
  resolveCustomWindow,
  resolvePreset,
  toIsoZ,
  type TimeRangeValue,
} from '@/lib/timeRange'
import type { LogViewerStatus, UseLogsResult } from '@/components/logs/types'

// D-HALOGS-SELECTOR: scope to the EXACT homeassistant container. Do NOT use a
// wildcard — it would catch the unrelated `grocy-homeassistant` container.
const HA_SERVICES_CSV = identitiesToServicesCsv([
  { source_type: 'docker', service: 'homeassistant' },
])

// D-HALOGS-DEFAULT-QUERY (Option B): errors-focused default. severity is a
// structured field populated on HA lines (error/warn/info/debug/critical).
const ERRORS_EXPR = 'severity:error OR severity:warn'
// Codebase match-all convention (see logsQlTranslate.translateSearchToLogsQl,
// LogsExplorerBody SURROUNDING_EXPR): the literal '*'.
const ALL_EXPR = '*'

const EMPTY_COPY =
  'No matching Home Assistant log lines in the selected range. Try widening the time window or switching to All lines.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

export function HomeAssistantLogsTab(): JSX.Element {
  const [wrap, setWrap] = useState(false)
  const [timezone, toggleTimezone] = useTimezonePreference()
  // Bumping this re-resolves an OPEN end against a fresh "now" (Refresh), without
  // churning the query key every render. Mirrors the Docker/Explorer viewers.
  const [refreshNonce, setRefreshNonce] = useState(0)
  // D-HALOGS-DEFAULT-QUERY: errors-only is the DEFAULT (true). The toggle flips
  // to All lines (match-all '*').
  const [errorsOnly, setErrorsOnly] = useState(true)
  // Default 1h window; the TimeRangeControl can change it (preset or custom).
  const [range, setRange] = useState<TimeRangeValue>({ kind: 'preset', token: '1h' })

  const expr = errorsOnly ? ERRORS_EXPR : ALL_EXPR

  // Resolve the committed range to absolute [startIso, endIso]. `now` must stay
  // STABLE across renders (else an open end re-reads new Date() each render →
  // query-key churn → refetch loop). Memoize on the range + refreshNonce so
  // `now` only advances on an explicit Refresh or range change. Mirrors
  // LogsExplorerBody (lines 234-248).
  const rangeKind = range.kind
  const rangeToken = range.kind === 'preset' ? range.token : undefined
  const rangeStartTime = range.kind === 'custom' ? (range.start?.getTime() ?? null) : null
  const rangeEndTime = range.kind === 'custom' ? (range.end?.getTime() ?? null) : null

  const { startIso, endIso } = useMemo(() => {
    const now = new Date()
    const win =
      rangeKind === 'preset'
        ? resolvePreset(rangeToken!, now)
        : resolveCustomWindow(
            {
              start: rangeStartTime !== null ? new Date(rangeStartTime) : undefined,
              end: rangeEndTime !== null ? new Date(rangeEndTime) : undefined,
            },
            { now, maxSpanDays: 30 },
          )
    return { startIso: toIsoZ(win.start), endIso: toIsoZ(win.end) }
    // eslint-disable-next-line react-hooks/exhaustive-deps, @eslint-react/exhaustive-deps -- intentional: refreshNonce re-resolves the window (fresh `now`) on explicit refresh
  }, [rangeKind, rangeToken, rangeStartTime, rangeEndTime, refreshNonce])

  const logs = useLogsQuery(expr, startIso, endIso, HA_SERVICES_CSV)

  const handleRefresh = (): void => {
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  // The generic /api/logs/query endpoint surfaces VictoriaLogs unavailability as
  // HTTP 502 (NOT 503 — that is the docker endpoint). Everything else non-2xx is
  // a generic error. Mirrors LogsExplorerBody (lines 418-419).
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  // pages[0] is the NEWEST window; reverse so oldest renders first (mirrors the
  // Docker/Explorer viewers). LogsQueryResponse has NO log_status/truncated, so
  // we derive logStatus below from line presence.
  const flatLines = useMemo(
    () =>
      (logs.data?.pages ?? [])
        .slice()
        .reverse()
        .flatMap((p) => p.lines),
    [logs.data],
  )
  const hasData = logs.data !== undefined

  // Open-in-Explorer deep-link: scope to the HA service (reusing HA_SERVICES_CSV,
  // the single source of truth) + carry the current range. For errors-only we
  // deep-link with the severity expr; for All lines we omit logsQl so the Explorer
  // opens unfiltered. The HA service scope is always set.
  const explorerLogsQl = errorsOnly ? ERRORS_EXPR : undefined
  const explorerServiceCsv = HA_SERVICES_CSV
  const explorerRange =
    range.kind === 'preset'
      ? { sincePreset: range.token }
      : {
          ...(range.start !== undefined ? { rangeStart: range.start } : {}),
          ...(range.end !== undefined ? { rangeEnd: range.end } : {}),
        }

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Home Assistant logs</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setErrorsOnly((v) => !v)}
          data-testid="ha-logs-errors-toggle"
          aria-pressed={errorsOnly}
        >
          {errorsOnly ? 'Errors only' : 'All lines'}
        </Button>
        <OpenInExplorerButton
          {...(explorerLogsQl !== undefined ? { logsQl: explorerLogsQl } : {})}
          selectedServices={[explorerServiceCsv]}
          {...explorerRange}
        />
        <WrapToggle checked={wrap} onChange={setWrap} id="ha-logs-wrap" />
        <TimezoneToggle
          checked={timezone === 'utc'}
          onChange={toggleTimezone}
          id="ha-logs-tz-toggle"
        />
        <TimeRangeControl value={range} onChange={setRange} presets={ALL_PRESETS} />
        <Button
          size="sm"
          variant="outline"
          onClick={handleRefresh}
          disabled={logs.isFetching}
          data-testid="ha-logs-refresh"
        >
          <RefreshCw className="mr-1 size-4" />
          {logs.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  )

  // Adapter: map the generic infinite-query result into UseLogsResult. Shape
  // mirrors LogsExplorerBody's NORMAL-mode branch (lines 1000-1038), minus the
  // windowed buffer / tail / surrounding extras. Field names match types.ts
  // verbatim — do NOT invent fields.
  const getLogsResult = (): UseLogsResult => {
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
    // Derive logStatus from line presence (LogsQueryResponse has no log_status):
    //   no data yet + no lines  -> undefined (shows the Loading state)
    //   data present, 0 lines    -> 'no_lines'
    //   data present, >0 lines   -> 'available'
    const status: LogViewerStatus | undefined =
      !hasData && flatLines.length === 0
        ? undefined
        : flatLines.length === 0
          ? 'no_lines'
          : 'available'
    return {
      lines: flatLines,
      isLoading: logs.isLoading && flatLines.length === 0,
      isError: false,
      error: undefined,
      logStatus: status,
      hasMore: logs.hasNextPage,
      isLoadingOlder: logs.isFetchingNextPage,
      loadOlder: () => {
        void logs.fetchNextPage()
      },
    }
  }

  return (
    <div className="p-4">
      <LogViewer
        useLogs={getLogsResult}
        headerSlot={header}
        emptyStateCopy={EMPTY_COPY}
        unavailableCopy={UNAVAILABLE_COPY}
        wrap={wrap}
        timezone={timezone}
      />
    </div>
  )
}
