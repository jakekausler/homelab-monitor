import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useMetricsRange } from '@/api/queries'
import type { Schema } from '@/api/types'

type MetricsRangeResponse = Schema<'MetricsRangeResponse'>

export interface RangeSeries {
  /** PromQL expression queried via /api/metrics/range. */
  expr: string
  /** Legend / tooltip label. */
  label: string
  /** Line stroke color. Defaults applied by index if omitted. */
  color?: string
}

export interface UnifiRangeChartProps {
  /** Accessible chart title (rendered as caption + used for aria-label). */
  title: string
  /** One or more series, each its own line on the same Y axis. */
  series: [RangeSeries, ...RangeSeries[]]
  /** Formats a Y value for the axis tick + tooltip (e.g. Mbps, ms). */
  valueFormatter: (value: number) => string
  /** Lookback window in seconds. Default 86400 (24h). */
  lookbackSeconds?: number
  /** Resolution step. Default '10m'. */
  step?: string
}

interface ChartRow {
  t: number
  label: string
  v0?: number
  v1?: number
}

function parseSeries(resp: MetricsRangeResponse | undefined): Map<number, number> {
  const out = new Map<number, number>()
  if (resp === undefined) return out
  // Network metrics are single-series (no per-label fan-out); take the first result.
  const result = resp.data.result[0]
  if (result === undefined) return out
  for (const pair of result.values) {
    if (pair.length < 2) continue
    const tsRaw = pair[0]
    const valRaw = pair[1]
    const ts = typeof tsRaw === 'string' ? Number(tsRaw) : tsRaw
    const val = typeof valRaw === 'string' ? Number(valRaw) : valRaw
    if (!Number.isFinite(ts) || !Number.isFinite(val)) continue
    out.set(ts as number, val as number)
  }
  return out
}

export function UnifiRangeChart({
  title,
  series,
  valueFormatter,
  lookbackSeconds = 86400,
  step = '10m',
}: UnifiRangeChartProps) {
  const [{ start, end }] = useState(() => {
    const endMs = Date.now()
    const startMs = endMs - lookbackSeconds * 1000
    return { start: new Date(startMs).toISOString(), end: new Date(endMs).toISOString() }
  })

  // Fixed two calls; never map the hook over variable-length array.
  const s0 = series[0]
  const s1 = series[1]
  const r0 = useMetricsRange(s0.expr, start, end, step)
  const r1 = useMetricsRange(s1?.expr ?? s0.expr, start, end, step)

  const rows = useMemo(() => {
    const m0 = parseSeries(r0.data)
    const m1 = parseSeries(r1.data)
    const timestamps = new Set([...m0.keys(), ...m1.keys()])
    const sortedTs = Array.from(timestamps).sort((a, b) => a - b)

    return sortedTs.map((t) => {
      const date = new Date(t * 1000)
      const label = date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
      const row: ChartRow = { t, label }
      if (m0.has(t)) row.v0 = m0.get(t)!
      if (s1 !== undefined && m1.has(t)) row.v1 = m1.get(t)!
      return row
    })
  }, [r0.data, r1.data, s1])

  const hasAny = rows.length > 0 && rows.some((r) => r.v0 !== undefined || r.v1 !== undefined)

  if (r0.isPending || (s1 !== undefined && r1.isPending)) {
    return (
      <div
        data-testid="range-chart-loading"
        className="flex items-center justify-center rounded-md bg-muted/30 p-8"
        style={{ height: 140 }}
      >
        <div className="animate-pulse text-sm text-muted-foreground">Loading…</div>
      </div>
    )
  }

  if (r0.isError || (s1 !== undefined && r1.isError)) {
    return (
      <div
        data-testid="range-chart-error"
        className="rounded-md bg-muted/30 p-3 text-sm text-muted-foreground"
      >
        History unavailable.
      </div>
    )
  }

  if (!hasAny) {
    return (
      <div
        data-testid="range-chart-empty"
        className="rounded-md bg-muted/30 p-6 text-center text-sm text-muted-foreground"
      >
        No {title} data in the last 24 hours.
      </div>
    )
  }

  return (
    <figure data-testid="range-chart" aria-label={title}>
      <figcaption className="mb-1 text-xs font-medium text-muted-foreground">{title}</figcaption>
      <div className="w-full" style={{ height: 140 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10 }} width={44} tickFormatter={valueFormatter} />
            <Tooltip formatter={(value) => valueFormatter(Number(value ?? 0))} />
            <Line
              type="monotone"
              dataKey="v0"
              name={s0.label}
              stroke={s0.color ?? '#2563eb'}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
            {s1 !== undefined && (
              <Line
                type="monotone"
                dataKey="v1"
                name={s1.label}
                stroke={s1.color ?? '#16a34a'}
                dot={false}
                connectNulls
                isAnimationActive={false}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  )
}
