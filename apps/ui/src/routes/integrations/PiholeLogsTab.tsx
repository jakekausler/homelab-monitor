// STAGE-006-024 — Pi-hole Logs tab: the shared <LogViewer> scoped to the
// `docker:pihole-unbound` stream. Mirrors HomeAssistantLogsTab exactly, with
// two key differences:
//   1. Service scope is `pihole-unbound` (FTL docker-stdout via pihole-unbound container).
//   2. Errors expression uses TEXT-MATCH LogsQL (`WARNING OR ERROR OR CRITICAL`) NOT
//      `severity:error OR severity:warn` — verified live that FTL lines all land as
//      severity:"info" because Vector's regex doesn't parse FTL's `[57/T70] WARNING:`
//      format, so a severity filter returns EMPTY.
//
// Default view: errors-focused text-match over 1h.
// An "Errors only ↔ All lines" toggle swaps expr between that and match-all '*'.
//
// SCAFFOLDING: Tier-3 per-query-feed toggle added in STAGE-006-025 (wires
// query_feed_streaming + the pihole-queries stream into this component). No
// toggle UI, no flag reads, and no dead code for it lives here.
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

// D-PIHOLELOGS-SELECTOR: scope to the pihole-unbound container only.
const PIHOLE_SERVICES_CSV = identitiesToServicesCsv([
  { source_type: 'docker', service: 'pihole-unbound' },
])

// D-PIHOLELOGS-DEFAULT-QUERY: text-match — FTL stdout lines do NOT have
// structured severity (Vector's regex doesn't parse `[57/T70] WARNING:` format),
// so all land as severity:"info". A severity filter returns EMPTY. Use LogsQL
// word/substring match on the message body instead.
// Pre-flight: verify `WARNING OR ERROR OR CRITICAL` hits live FTL lines. If VictoriaLogs
// requires word-match syntax, use `word(WARNING) OR word(ERROR) OR word(CRITICAL)`.
const ERRORS_EXPR = 'WARNING OR ERROR OR CRITICAL'
// Codebase match-all convention (mirrors HA + LogsExplorerBody SURROUNDING_EXPR).
const ALL_EXPR = '*'

const EMPTY_COPY =
  'No matching Pi-hole (FTL) log lines in the selected range. Try widening the time window or switching to All lines.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

export function PiholeLogsTab(): JSX.Element {
  const [wrap, setWrap] = useState(false)
  const [timezone, toggleTimezone] = useTimezonePreference()
  // Bumping this re-resolves an OPEN end against a fresh "now" (Refresh), without
  // churning the query key every render. Mirrors the Docker/Explorer/HA viewers.
  const [refreshNonce, setRefreshNonce] = useState(0)
  // Default: errors-only ON (mirrors HA). Toggle flips to All lines ('*').
  const [errorsOnly, setErrorsOnly] = useState(true)
  // Default 1h window; the TimeRangeControl can change it (preset or custom).
  const [range, setRange] = useState<TimeRangeValue>({ kind: 'preset', token: '1h' })

  const expr = errorsOnly ? ERRORS_EXPR : ALL_EXPR

  // Resolve the committed range to absolute [startIso, endIso]. `now` must stay
  // STABLE across renders (else an open end re-reads new Date() each render →
  // query-key churn → refetch loop). Memoize on the range + refreshNonce so
  // `now` only advances on an explicit Refresh or range change. Mirrors
  // LogsExplorerBody (lines 234-248) and HomeAssistantLogsTab exactly.
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

  const logs = useLogsQuery(expr, startIso, endIso, PIHOLE_SERVICES_CSV)

  const handleRefresh = (): void => {
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  // The generic /api/logs/query endpoint surfaces VictoriaLogs unavailability as
  // HTTP 502 (NOT 503). Mirrors HomeAssistantLogsTab and LogsExplorerBody.
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  // pages[0] is the NEWEST window; reverse so oldest renders first.
  const flatLines = useMemo(
    () =>
      (logs.data?.pages ?? [])
        .slice()
        .reverse()
        .flatMap((p) => p.lines),
    [logs.data],
  )
  const hasData = logs.data !== undefined

  // Open-in-Explorer deep-link: scope to the pihole-unbound service + carry the
  // current range. For errors-only we deep-link with the text-match expr; for
  // All lines we omit logsQl so the Explorer opens unfiltered.
  const explorerLogsQl = errorsOnly ? ERRORS_EXPR : undefined
  const explorerServiceCsv = PIHOLE_SERVICES_CSV
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
        <span className="font-medium">Pi-hole (FTL) logs</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setErrorsOnly((v) => !v)}
          data-testid="pihole-logs-errors-toggle"
          aria-pressed={errorsOnly}
        >
          {errorsOnly ? 'Errors only' : 'All lines'}
        </Button>
        <OpenInExplorerButton
          {...(explorerLogsQl !== undefined ? { logsQl: explorerLogsQl } : {})}
          selectedServices={[explorerServiceCsv]}
          {...explorerRange}
        />
        <WrapToggle checked={wrap} onChange={setWrap} id="pihole-logs-wrap" />
        <TimezoneToggle
          checked={timezone === 'utc'}
          onChange={toggleTimezone}
          id="pihole-logs-tz-toggle"
        />
        <TimeRangeControl value={range} onChange={setRange} presets={ALL_PRESETS} />
        <Button
          size="sm"
          variant="outline"
          onClick={handleRefresh}
          disabled={logs.isFetching}
          data-testid="pihole-logs-refresh"
        >
          <RefreshCw className="mr-1 size-4" />
          {logs.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  )

  // Adapter: map the generic infinite-query result into UseLogsResult. Shape
  // mirrors HomeAssistantLogsTab getLogsResult() verbatim. Field names match
  // types.ts — do NOT invent fields.
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
