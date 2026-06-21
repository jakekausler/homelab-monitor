import type { JSX } from 'react'

import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { formatRelative, formatUptime } from '@/lib/relativeTime'

type UnifiWanCurrent = Schema<'UnifiWanCurrent'>

/** Speedtest/xput values are already Mbps — format directly, do NOT re-divide. */
function formatMbps(value: number | null): string {
  if (value === null) return '—'
  return `${value.toFixed(1)} Mbps`
}

/** Latency/ping are SECONDS — render as ms (sub-second) or s. */
function formatSecondsMs(value: number | null): string {
  if (value === null) return '—'
  if (value < 1) return `${(value * 1000).toFixed(0)} ms`
  return `${value.toFixed(2)} s`
}

/** speedtest_lastrun is a UNIX-SECONDS timestamp; render relative. */
function formatLastrun(unixSeconds: number | null): string {
  if (unixSeconds === null) return '—'
  return formatRelative(new Date(unixSeconds * 1000).toISOString())
}

function Row({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex justify-between gap-4 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground tabular-nums">{value}</span>
    </div>
  )
}

export function NetworkWanWidget({ wan }: { wan: UnifiWanCurrent }): JSX.Element {
  return (
    <div className="space-y-1">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant={wan.wan_up ? 'ok' : 'critical'}>WAN {wan.wan_up ? 'up' : 'down'}</Badge>
        <Badge variant={wan.failover_capable ? 'ok' : 'muted'}>
          Failover {wan.failover_capable ? 'capable' : 'unavailable'}
        </Badge>
        {wan.failover_active && <Badge variant="warn">Failover active</Badge>}
      </div>
      <Row label="Download" value={formatMbps(wan.download_mbps)} />
      <Row label="Upload" value={formatMbps(wan.upload_mbps)} />
      <Row label="Throughput down" value={formatMbps(wan.xput_down_mbps)} />
      <Row label="Throughput up" value={formatMbps(wan.xput_up_mbps)} />
      <Row label="Latency" value={formatSecondsMs(wan.latency_seconds)} />
      <Row label="Ping" value={formatSecondsMs(wan.ping_seconds)} />
      <Row label="WAN uptime" value={formatUptime(wan.wan_uptime_seconds)} />
      <Row label="Last speedtest" value={formatLastrun(wan.speedtest_lastrun)} />
    </div>
  )
}
