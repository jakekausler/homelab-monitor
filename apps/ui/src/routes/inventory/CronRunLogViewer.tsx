import { useState } from 'react'
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { cronQueryKeys, useCronRunLog } from '@/api/crons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { RunStateBadge } from '@/components/crons/badges'
import { formatDuration } from '@/lib/relativeTime'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import { fieldFilterClause } from '@/lib/logsQlTranslate'
import { parseIso, toIsoZ, type PresetToken, type TimeRangeValue } from '@/lib/timeRange'
import type { UseLogsResult } from '@/components/logs/types'

const RUN_ID_DISPLAY_PREFIX = 12

export function CronRunLogViewerPage() {
  const params = useParams({ strict: false })
  const fingerprint = params.fingerprint ?? ''
  const runId = params.run_id ?? ''
  const log = useCronRunLog(fingerprint, runId)
  const qc = useQueryClient()
  const [wrap, setWrap] = useState(false)
  // STAGE-004-009 timezone wiring; Explorer (STAGE-004-010) mirrors this.
  const [timezone, toggleTimezone] = useTimezonePreference()

  const search = useSearch({ strict: false })
  const navigate = useNavigate()

  const isUnavailable = log.error instanceof ApiError && log.error.status === 503
  const isGenericError = log.error instanceof ApiError && !isUnavailable

  const handleRefresh = () => {
    void qc.invalidateQueries({ queryKey: cronQueryKeys.runLog(fingerprint, runId) })
  }

  const pages = log.data?.pages ?? []
  const firstPage = pages[0]
  // pages accumulate newest-first (pages[0] = newest window, later pages =
  // OLDER via "Load older"). Each page is internally oldest->newest, so flatten
  // in REVERSE page order to render globally oldest->newest (older pages on top).
  const allLines = pages
    .slice()
    .reverse()
    .flatMap((p) => p.lines)

  // STAGE-004-008 — the run window has NO backend field on RunLogResponse, so
  // derive [min, max] from the flattened line timestamps (earliest → latest).
  const lineTimes = allLines.map((l) => parseIso(l.timestamp)).filter((d): d is Date => d !== null)
  const runMin =
    lineTimes.length > 0 ? new Date(Math.min(...lineTimes.map((d) => d.getTime()))) : undefined
  const runMax =
    lineTimes.length > 0 ? new Date(Math.max(...lineTimes.map((d) => d.getTime()))) : undefined

  // Selected narrow window from URL (custom) — defaults to full run window.
  const selStart = search.start !== undefined ? parseIso(search.start) : null
  const selEnd = search.end !== undefined ? parseIso(search.end) : null
  const hasNarrow = selStart !== null || selEnd !== null

  // Client-side filter: resolve open bounds to the run window, then filter.
  const flatLines = hasNarrow
    ? allLines.filter((l) => {
        const t = parseIso(l.timestamp)
        if (t === null) return false
        const filterStart = selStart ?? runMin
        const filterEnd = selEnd ?? runMax
        if (filterStart === undefined || filterEnd === undefined) return true
        return t.getTime() >= filterStart.getTime() && t.getTime() <= filterEnd.getTime()
      })
    : allLines

  const rangeValue: TimeRangeValue = hasNarrow
    ? { kind: 'custom', start: selStart ?? undefined, end: selEnd ?? undefined }
    : runMin !== undefined && runMax !== undefined
      ? { kind: 'custom', start: runMin, end: runMax }
      : { kind: 'preset', token: '15m' }

  const handleRangeChange = (v: TimeRangeValue): void => {
    if (v.kind === 'custom') {
      void navigate({
        to: '/inventory/crons/$fingerprint/runs/$run_id',
        params: { fingerprint, run_id: runId },
        search: {
          start: v.start !== undefined ? toIsoZ(v.start) : undefined,
          end: v.end !== undefined ? toIsoZ(v.end) : undefined,
        },
      })
    } else {
      // Preset in bounded mode is unusual; treat as "clear narrowing".
      void navigate({
        to: '/inventory/crons/$fingerprint/runs/$run_id',
        params: { fingerprint, run_id: runId },
        search: { start: undefined, end: undefined },
      })
    }
  }

  // STAGE-004-021 — props for the "Open in Explorer" deep-link. LogsQL targets
  // this exact run; the time range follows the locked precedence:
  //   1. user-narrowed (either search.start OR search.end present) → that range
  //      (single-sided narrow: open bound stays open in the Explorer link)
  //   2. full run window (runMin AND runMax) → padded ±1s
  //   3. loading/empty (no runMin) → fall back to the 1h preset
  // selStart/selEnd are Date | null (parsed from the URL); runMin/runMax are
  // Date | undefined (both derive from the same log-line array, so they are
  // either both defined or both undefined — a runMin-only state cannot arise).
  // Build the props with spread-conditionals so no key is ever explicitly set
  // to undefined (exactOptionalPropertyTypes).
  const explorerTimeProps: {
    sincePreset?: PresetToken
    rangeStart?: Date
    rangeEnd?: Date
  } =
    selStart !== null || selEnd !== null
      ? {
          ...(selStart !== null ? { rangeStart: selStart } : {}),
          ...(selEnd !== null ? { rangeEnd: selEnd } : {}),
        }
      : runMin !== undefined && runMax !== undefined
        ? {
            rangeStart: new Date(runMin.getTime() - 1000),
            rangeEnd: new Date(runMax.getTime() + 1000),
          }
        : { sincePreset: '1h' }

  const header = (
    <>
      <Link
        to="/inventory/crons/$fingerprint/runs"
        params={{ fingerprint }}
        search={{ cursor: undefined, state: undefined }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to runs
      </Link>
      <header
        className="sticky top-0 z-10 -mx-4 space-y-2 border-b border-border bg-background/95 px-4 py-3 backdrop-blur"
        data-testid="run-log-header"
      >
        {isGenericError && (
          <p role="alert" className="text-red-600">
            {log.error?.message}
          </p>
        )}
        {firstPage && (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <RunLogHeader
              data={firstPage}
              runId={runId}
              onRefresh={handleRefresh}
              isRefreshing={log.isFetching}
            />
            <div className="flex items-center gap-2">
              <OpenInExplorerButton
                logsQl={`${fieldFilterClause('cron_fingerprint', fingerprint)!} AND ${fieldFilterClause('run_id', runId)!}`}
                {...explorerTimeProps}
              />
              {runMin !== undefined && runMax !== undefined && (
                <TimeRangeControl
                  value={rangeValue}
                  onChange={handleRangeChange}
                  mode="bounded"
                  min={runMin}
                  max={runMax}
                  presets={[]}
                />
              )}
              <WrapToggle checked={wrap} onChange={setWrap} id="cron-wrap" />
              <TimezoneToggle
                checked={timezone === 'utc'}
                onChange={toggleTimezone}
                id="cron-tz-toggle"
              />
            </div>
          </div>
        )}
      </header>
    </>
  )

  const useLogs = (): UseLogsResult => {
    if (isUnavailable) {
      return {
        lines: undefined,
        isLoading: false,
        isError: true,
        error: log.error instanceof ApiError ? log.error : undefined,
        logStatus: 'unavailable',
      }
    }
    if (isGenericError) {
      return { lines: undefined, isLoading: false, isError: false, error: undefined }
    }
    return {
      lines: flatLines,
      isLoading: log.isLoading,
      isError: false,
      error: undefined,
      logStatus:
        firstPage?.log_status === 'expired'
          ? 'expired'
          : firstPage?.log_status === 'running'
            ? 'running'
            : firstPage?.log_status === 'available'
              ? 'available'
              : undefined,
      truncated: firstPage?.truncated,
      hasMore: log.hasNextPage,
      isLoadingOlder: log.isFetchingNextPage,
      loadOlder: () => {
        void log.fetchNextPage()
      },
    }
  }

  return (
    <LogViewer
      useLogs={useLogs}
      headerSlot={header}
      emptyStateCopy="No log lines captured for this run."
      unavailableCopy="The log backend is temporarily unavailable. The run still happened — its metadata will appear here once the backend recovers."
      wrap={wrap}
      timezone={timezone}
    />
  )
}

function RunLogHeader({
  data,
  runId,
  onRefresh,
  isRefreshing,
}: {
  data: import('@/api/types').Schema<'RunLogResponse'>
  runId: string
  onRefresh: () => void
  isRefreshing: boolean
}) {
  const flags = data.anomaly_flags.length > 0 ? data.anomaly_flags.split(',') : []
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs" title={runId}>
          {runId.length > RUN_ID_DISPLAY_PREFIX
            ? `${runId.slice(0, RUN_ID_DISPLAY_PREFIX)}…`
            : runId}
        </code>
        <RunStateBadge state={data.state} />
        <span className="text-xs text-muted-foreground">
          {formatDuration(data.duration_seconds)}
        </span>
        {data.lines.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {String(data.line_count ?? data.lines.length)} lines
          </span>
        )}
        {flags.map((f) => (
          <Badge key={f} variant="warn" data-testid="anomaly-badge">
            {f}
          </Badge>
        ))}
      </div>
      {data.log_status === 'running' && (
        <Button
          size="sm"
          variant="outline"
          onClick={onRefresh}
          disabled={isRefreshing}
          data-testid="refresh-log"
        >
          <RefreshCw className="mr-1 size-4" />
          {isRefreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      )}
    </div>
  )
}
