import { useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Sparkline } from '@/components/tiles/Sparkline'
import { useMetricsSnapshot } from '@/api/queries'
import { useSSE } from '@/lib/sse'

const HOST_CPU_METRIC = 'homelab_host_cpu_percent'
const HOST_CPU_LABEL = 'all'
const SERIES_CAPACITY = 60

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

  // Seed once from /api/metrics/snapshot on first successful fetch.
  useEffect(() => {
    if (seededRef.current) return
    if (snapshot.data === undefined) return
    const entry = snapshot.data.entries.find(
      (e) => e.name === HOST_CPU_METRIC && e.labels['cpu'] === HOST_CPU_LABEL,
    )
    if (entry !== undefined) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
      setLatest(entry.value)
      setSeries([entry.value])
      // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
      setLastUpdatedAt(entry.ts)
    }
    seededRef.current = true
  }, [snapshot.data])

  // On every host tick, re-fetch the snapshot to pick up the new CPU value.
  // SCAFFOLDING: in STAGE-001-015 we replace the snapshot fetch with a
  // direct VictoriaMetrics query. For now, the tick event is the trigger.
  useEffect(() => {
    if (sse.value === null) return
    void snapshot.refetch()
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
    setLastUpdatedAt(sse.value.ts)
  }, [sse.value, snapshot])

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
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: seeding/updating state from non-React source (snapshot, SSE event)
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
                <p className="mt-1 text-xs text-muted-foreground">Updated {lastUpdatedAt}</p>
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
