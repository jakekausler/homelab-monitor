import { useMemo } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { MouseHandlerDataParam } from 'recharts'

import { useLogsHistogramQuery } from '@/api/logs'
import { SEVERITY_BAR_COLORS } from '@/components/logs/severity'

interface HistogramChartProps {
  expr: string
  start: string
  end: string
  buckets?: number
  services?: string
  /** Narrow the time range to [startIso, endIso) for the clicked bucket. */
  onNarrowRange: (startIso: string, endIso: string) => void
}

interface ChartRow {
  label: string
  error: number
  warn: number
  info: number
  total: number
  startMs: number
}

const CHART_HEIGHT = 96

/** Short HH:MM label for a bucket start (local time). */
function bucketLabel(startMs: number): string {
  const d = new Date(startMs)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function HistogramChart({
  expr,
  start,
  end,
  buckets = 60,
  services = '',
  onNarrowRange,
}: HistogramChartProps) {
  const query = useLogsHistogramQuery(expr, start, end, buckets, services)
  const durationMs = query.data?.bucket_duration_ms ?? 0

  const rows: ChartRow[] = useMemo(() => {
    const data = query.data
    if (data === undefined) return []
    return data.buckets.map((b) => {
      const startMs = new Date(b.start_ts).getTime()
      return {
        label: bucketLabel(startMs),
        error: b.counts_by_severity.error ?? 0,
        warn: b.counts_by_severity.warn ?? 0,
        info: b.counts_by_severity.info ?? 0,
        total: b.total,
        startMs,
      }
    })
  }, [query.data])

  // BarChart onClick provides the active data row index; map to its bucket window.
  const handleClick = (state: MouseHandlerDataParam): void => {
    const rawIdx = state.activeTooltipIndex
    const idx = typeof rawIdx === 'number' ? rawIdx : null
    if (idx === null) return
    const row = rows[idx]
    if (row === undefined || durationMs <= 0) return
    const startIso = new Date(row.startMs).toISOString()
    const endIso = new Date(row.startMs + durationMs).toISOString()
    onNarrowRange(startIso, endIso)
  }

  if (query.isLoading) {
    return (
      <div
        data-testid="histogram-loading"
        className="h-24 w-full animate-pulse rounded bg-muted"
        style={{ height: CHART_HEIGHT }}
      />
    )
  }

  // Suppress the chart on error; a small inline alert keeps the layout calm.
  if (query.isError) {
    return (
      <div data-testid="histogram-error" role="alert" className="px-1 py-2 text-xs text-red-600">
        Histogram unavailable.
      </div>
    )
  }

  const hasAny = rows.some((r) => r.total > 0)
  if (rows.length === 0 || !hasAny) {
    return (
      <div data-testid="histogram-empty" className="px-1 py-2 text-xs text-muted-foreground">
        No log activity in the selected range.
      </div>
    )
  }

  return (
    <div data-testid="histogram-chart" className="w-full" style={{ height: CHART_HEIGHT }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={rows}
          margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
          onClick={handleClick}
        >
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 10 }} width={28} allowDecimals={false} />
          <Tooltip />
          <Bar dataKey="error" stackId="s" fill={SEVERITY_BAR_COLORS.error} />
          <Bar dataKey="warn" stackId="s" fill={SEVERITY_BAR_COLORS.warn} />
          <Bar dataKey="info" stackId="s" fill={SEVERITY_BAR_COLORS.info} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
