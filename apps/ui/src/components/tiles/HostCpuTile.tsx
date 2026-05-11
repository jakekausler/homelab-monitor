import { useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Sparkline } from '@/components/tiles/Sparkline'
import { useMetricsRange, useMetricsSnapshot } from '@/api/queries'
import { useSSE } from '@/lib/sse'

const HOST_CPU_METRIC = 'homelab_host_cpu_percent'
const HOST_CPU_LABEL = 'all'
const SERIES_CAPACITY = 60
const RANGE_LOOKBACK_S = 600 // 10 minutes
const RANGE_STEP = '10s'
const RANGE_EXPR = `${HOST_CPU_METRIC}{cpu="${HOST_CPU_LABEL}"}`

/**
 * Compute fixed start/end ISO strings for the initial range query.
 * Compute once per mount via useState initializer so the
 * query key is stable across renders (useState initializer runs exactly once
 * per mount, unlike useMemo which React may drop).
 */
function rangeWindow(): { start: string; end: string } {
  const endMs = Date.now()
  const startMs = endMs - RANGE_LOOKBACK_S * 1000
  return {
    start: new Date(startMs).toISOString(),
    end: new Date(endMs).toISOString(),
  }
}

/**
 * Convert a VM matrix `values` array into a SERIES_CAPACITY-length number[].
 * Pads short series with the first value; trims long series to the trailing
 * SERIES_CAPACITY samples. Returns null if the array is empty/unparseable.
 */
function buildSeriesFromVMValues(
  values: ReadonlyArray<ReadonlyArray<number | string>>,
): number[] | null {
  const parsed: number[] = []
  for (const pair of values) {
    if (pair.length < 2) continue
    const raw = pair[1]!
    const n = typeof raw === 'string' ? Number(raw) : raw
    if (Number.isFinite(n)) parsed.push(n)
  }
  if (parsed.length === 0) return null
  if (parsed.length >= SERIES_CAPACITY) {
    return parsed.slice(parsed.length - SERIES_CAPACITY)
  }
  // Pad start with the first sample so the sparkline starts visually steady.
  const padded = Array<number>(SERIES_CAPACITY - parsed.length).fill(parsed[0]!)
  return [...padded, ...parsed]
}

function formatUpdatedAt(iso: string): string {
  // Backend timestamps are ISO-8601 UTC. Format in the user's local TZ.
  // Show HH:MM:SS only if same calendar day (local TZ), otherwise prefix
  // with YYYY-MM-DD so older timestamps disambiguate.
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  const ss = String(date.getSeconds()).padStart(2, '0')
  const time = `${hh}:${mm}:${ss}`
  if (sameDay) return time
  const yy = date.getFullYear()
  const mo = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  return `${yy}-${mo}-${dd} ${time}`
}

interface TickPayload {
  kind: 'collector.tick'
  collector: string
  outcome: 'success' | 'failure' | 'shutdown' | 'skipped'
  ts: string
  // other fields ignored
}

function parseTickEvent(event: MessageEvent<string>): TickPayload | null {
  try {
    const obj: unknown = JSON.parse(event.data)
    if (typeof obj !== 'object' || obj === null) return null
    const candidate = obj as Partial<TickPayload>
    if (candidate.kind !== 'collector.tick') return null
    if (candidate.collector !== 'host') return null
    if (typeof candidate.ts !== 'string') return null
    return candidate as TickPayload
  } catch {
    return null
  }
}

export function HostCpuTile() {
  const snapshot = useMetricsSnapshot()
  const sse = useSSE<TickPayload>({
    topic: 'collector.tick',
    parser: parseTickEvent,
  })

  const [series, setSeries] = useState<number[]>([])
  const [latest, setLatest] = useState<number | null>(null)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null)
  const seededRef = useRef(false)
  const historyBackfilledRef = useRef(false)

  // Compute window ONCE per mount; use useState initializer for per-mount guarantee.
  const [{ start: rangeStart, end: rangeEnd }] = useState(() => rangeWindow())
  const range = useMetricsRange(RANGE_EXPR, rangeStart, rangeEnd, RANGE_STEP)

  // Seed once from /api/metrics/snapshot on first successful fetch.
  useEffect(() => {
    if (seededRef.current) return
    if (snapshot.data === undefined) return
    const entry = snapshot.data.entries.find(
      (e) => e.name === HOST_CPU_METRIC && e.labels['cpu'] === HOST_CPU_LABEL,
    )
    if (entry !== undefined) {
      // eslint-disable-next-line react-hooks/set-state-in-effect,@eslint-react/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
      setLatest(entry.value)
      setSeries(Array<number>(SERIES_CAPACITY).fill(entry.value))
      // eslint-disable-next-line @eslint-react/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
      setLastUpdatedAt(entry.ts)
    }
    seededRef.current = true
  }, [snapshot.data])

  // Optimistic backfill: replace synthetic series with VM history once.
  // Fires AT MOST once per mount. If SSE has already started feeding live
  // ticks (historyBackfilledRef set by the SSE path's first tick — see
  // below), we skip — live data is more valuable than backfill.
  useEffect(() => {
    if (historyBackfilledRef.current) return
    if (range.data === undefined) return
    const result = range.data.data.result.find((r) => r.metric['cpu'] === HOST_CPU_LABEL)
    if (result === undefined) return
    const built = buildSeriesFromVMValues(result.values)
    if (built === null) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding state from one-shot historical query
    setSeries(built)

    setLatest(built[built.length - 1]!)
    historyBackfilledRef.current = true
  }, [range.data])

  // On every host tick, re-fetch the snapshot to pick up the new CPU value.
  // SCAFFOLDING: in STAGE-001-015 we replace the snapshot fetch with a
  // direct VictoriaMetrics query. For now, the tick event is the trigger.
  // Latest-ref for snapshot.refetch: TanStack Query returns a new `snapshot`
  // object every render; including it in the dep array would loop. We only
  // want this effect to fire on a real SSE tick.
  const refetchRef = useRef(snapshot.refetch)
  // eslint-disable-next-line react-hooks/refs -- latest-ref pattern; safe because ref writes don't trigger re-render
  refetchRef.current = snapshot.refetch

  useEffect(() => {
    if (sse.value === null) return
    void refetchRef.current()
    // eslint-disable-next-line react-hooks/set-state-in-effect,@eslint-react/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
    setLastUpdatedAt(sse.value.ts)
  }, [sse.value])

  // Each refetch returns a new snapshot; append to the series.
  useEffect(() => {
    if (snapshot.data === undefined) return
    if (!seededRef.current) return
    const entry = snapshot.data.entries.find(
      (e) => e.name === HOST_CPU_METRIC && e.labels['cpu'] === HOST_CPU_LABEL,
    )
    if (entry === undefined) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
    setLatest(entry.value)

    setSeries((prev) => {
      const next = [...prev, entry.value]
      return next.length > SERIES_CAPACITY ? next.slice(next.length - SERIES_CAPACITY) : next
    })
  }, [snapshot.data])

  const isStale = sse.status === 'error'
  const isInitialLoading = latest === null && sse.status === 'connecting'

  const bigNumber = useMemo(() => {
    if (latest === null) return '—'
    return `${latest.toFixed(1)}%`
  }, [latest])

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Host CPU</CardTitle>
            <CardDescription>Last 60 samples</CardDescription>
          </div>
          {isStale && (
            <span
              role="status"
              className="rounded-md border border-status-warning bg-status-warning/10 px-2 py-0.5 text-xs font-medium text-status-warning"
            >
              stale
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isInitialLoading ? (
          <p className="text-sm text-muted-foreground">Connecting…</p>
        ) : (
          <div className="flex items-end justify-between gap-4">
            <div>
              <div className="text-4xl font-semibold tabular-nums" aria-label="Host CPU percent">
                {bigNumber}
              </div>
              {lastUpdatedAt !== null && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Updated {formatUpdatedAt(lastUpdatedAt)}
                </p>
              )}
            </div>
            <Sparkline values={series} ariaLabel="Host CPU history" />
          </div>
        )}
        {sse.failureCount >= 3 && (
          <div className="mt-4 flex items-center gap-3">
            <p className="text-sm text-status-warning">Lost connection to live updates.</p>
            <Button size="sm" variant="outline" onClick={sse.reconnect}>
              Reconnect
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
