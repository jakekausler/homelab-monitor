import type { JSX } from 'react'

import type { Schema } from '@/api/types'

import { formatBand, formatLink } from './unifiFormat'

type UnifiWifiResponse = Schema<'UnifiWifiResponse'>

function Distribution({
  title,
  rows,
  formatKey = (k: string) => k,
}: {
  title: string
  rows: { key: string; count: number }[]
  formatKey?: (key: string) => string
}): JSX.Element {
  if (rows.length === 0) {
    return (
      <div>
        <p className="text-xs font-medium text-muted-foreground">{title}</p>
        <p className="text-sm text-muted-foreground">No data</p>
      </div>
    )
  }
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{title}</p>
      <ul className="space-y-0.5 text-sm">
        {rows.map((r) => (
          <li key={r.key} className="flex justify-between">
            <span className="text-foreground">{formatKey(r.key)}</span>
            <span className="text-muted-foreground tabular-nums">{r.count}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function NetworkWifiWidget({ data }: { data: UnifiWifiResponse }): JSX.Element {
  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-3 gap-2">
        <div>
          <p className="text-xs text-muted-foreground">Poor signal</p>
          <p className="text-lg font-semibold tabular-nums">{data.poor_signal}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Poor satisfaction</p>
          <p className="text-lg font-semibold tabular-nums">{data.poor_satisfaction}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">High retries</p>
          <p className="text-lg font-semibold tabular-nums">{data.high_retries}</p>
        </div>
      </div>
      <Distribution title="By band" rows={data.by_band} formatKey={formatBand} />
      <Distribution title="By link" rows={data.by_link} formatKey={formatLink} />
    </div>
  )
}
