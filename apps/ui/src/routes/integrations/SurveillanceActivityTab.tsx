// STAGE-008-029 — Surveillance "Activity" tab. Per-camera + system activity
// widgets (events, recordings count + storage BYTES) above an embedded
// <LogViewer> scoped to the DSM-WIDE Synology syslog stream via the LogsQL
// regex expr `service:~"synology-.*"` (captures synology-auth + siblings).
// The services CSV is intentionally EMPTY — the regex scope lives in `expr`,
// NOT in the exact-pair `services` filter. Mirrors PiholeLogsTab's adapter.
import { useMemo, useState, type JSX } from 'react'
import { RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { useLogsQuery } from '@/api/logs'
import { useSurveillanceCameras } from '@/api/surveillance'
import type { Schema } from '@/api/types'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import {
  ALL_PRESETS,
  resolveCustomWindow,
  resolvePreset,
  toIsoZ,
  type TimeRangeValue,
} from '@/lib/timeRange'
import type { LogViewerStatus, UseLogsResult } from '@/components/logs/types'

import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'
import { formatBytes } from './unifiFormat'

type SurveillanceCameras = Schema<'SurveillanceCameras'>
type CameraRow = Schema<'CameraRow'>

// DSM-wide syslog: regex over the synology-* service family. Empty services CSV.
const SYNOLOGY_LOG_EXPR = 'service:~"synology-.*"'
const EMPTY_COPY =
  'No Synology DSM syslog lines in the selected range. This is DSM-wide syslog ' +
  '(not surveillance-specific) and may be sparse. Try widening the time window.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

/** Render a value or an em-dash for null/empty. Mirrors SurveillanceCamerasTab.dash. */
function dash(value: string | number | null): string {
  if (value === null) return '—'
  if (typeof value === 'string' && value.length === 0) return '—'
  return String(value)
}

/** A single labeled stat cell. */
function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

function ActivityWidgets({ data }: { data: SurveillanceCameras }): JSX.Element {
  if (!data.data_available) {
    return (
      <EmptyState testId="surveillance-activity-unavailable">
        No surveillance data yet — the collector has not run.
      </EmptyState>
    )
  }
  return (
    <div className="space-y-4">
      <PanelSection title="Activity">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Events today" value={dash(data.events_today)} />
          <Stat label="Events (all time)" value={dash(data.events_total_all)} />
          <Stat label="Recordings" value={dash(data.recordings_total)} />
          <Stat label="Recording storage" value={formatBytes(data.recordings_bytes_total)} />
        </dl>
      </PanelSection>
      <PanelSection title="Per-camera recordings">
        {data.cameras.length === 0 ? (
          <EmptyState testId="surveillance-activity-cameras-empty">0 cameras</EmptyState>
        ) : (
          <table className="w-full text-sm" data-testid="surveillance-activity-camera-table">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Camera</th>
                <th className="py-1 font-medium">Recordings</th>
                <th className="py-1 font-medium">Storage</th>
              </tr>
            </thead>
            <tbody>
              {data.cameras.map((camera: CameraRow) => (
                <tr
                  key={camera.camera}
                  data-testid={`surveillance-activity-row-${camera.camera}`}
                  className="border-t border-border"
                >
                  <td className="py-1 font-medium text-foreground">{camera.camera}</td>
                  <td className="py-1 tabular-nums">{dash(camera.recordings_count)}</td>
                  <td className="py-1 tabular-nums">{formatBytes(camera.recordings_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </PanelSection>
    </div>
  )
}

export function SurveillanceActivityTab(): JSX.Element {
  const cameras = useSurveillanceCameras()
  const [wrap, setWrap] = useState(false)
  const [timezone, toggleTimezone] = useTimezonePreference()
  const [refreshNonce, setRefreshNonce] = useState(0)
  const [range, setRange] = useState<TimeRangeValue>({ kind: 'preset', token: '1h' })

  // Resolve range to absolute [startIso, endIso]. `now` must stay stable across
  // renders (mirrors PiholeLogsTab) — only advances on Refresh or range change.
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

  // Regex scope in expr; EMPTY services CSV (do NOT pass a glob to services).
  const logs = useLogsQuery(SYNOLOGY_LOG_EXPR, startIso, endIso, '')

  const handleRefresh = (): void => {
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  const flatLines = useMemo(
    () =>
      (logs.data?.pages ?? [])
        .slice()
        .reverse()
        .flatMap((p) => p.lines),
    [logs.data],
  )
  const hasData = logs.data !== undefined

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-col gap-0.5">
        <span className="font-medium">Synology DSM syslog</span>
        <span className="text-xs text-muted-foreground">
          DSM-wide system syslog (not surveillance-specific).
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <WrapToggle checked={wrap} onChange={setWrap} id="surveillance-activity-logs-wrap" />
        <TimezoneToggle
          checked={timezone === 'utc'}
          onChange={toggleTimezone}
          id="surveillance-activity-logs-tz-toggle"
        />
        <TimeRangeControl value={range} onChange={setRange} presets={ALL_PRESETS} />
        <Button
          size="sm"
          variant="outline"
          onClick={handleRefresh}
          disabled={logs.isFetching}
          data-testid="surveillance-activity-logs-refresh"
        >
          <RefreshCw className="mr-1 size-4" />
          {logs.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  )

  // Adapter: map the infinite-query result into UseLogsResult. Mirrors
  // PiholeLogsTab.getLogsResult — field names match types.ts; do NOT invent fields.
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
      return { lines: undefined, isLoading: false, isError: false, error: undefined }
    }
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
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <QueryState
        result={cameras}
        unavailableLabel="Surveillance activity metrics temporarily unavailable"
        renderData={(data: SurveillanceCameras) => <ActivityWidgets data={data} />}
      />
      <div>
        <LogViewer
          useLogs={getLogsResult}
          headerSlot={header}
          emptyStateCopy={EMPTY_COPY}
          unavailableCopy={UNAVAILABLE_COPY}
          wrap={wrap}
          timezone={timezone}
        />
      </div>
    </div>
  )
}
