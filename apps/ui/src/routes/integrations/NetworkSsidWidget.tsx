import type { JSX } from 'react'

import type { Schema } from '@/api/types'

type UnifiWifiResponse = Schema<'UnifiWifiResponse'>

export function NetworkSsidWidget({ ssids }: { ssids: UnifiWifiResponse['ssids'] }): JSX.Element {
  if (ssids.length === 0) {
    return <p className="text-sm text-muted-foreground">No SSID client data</p>
  }
  const max = Math.max(...ssids.map((s) => s.count), 1)
  return (
    <ul className="space-y-2 text-sm">
      {ssids.map((s) => (
        <li key={s.ssid}>
          <div className="flex justify-between">
            <span className="text-foreground">{s.ssid}</span>
            <span className="text-muted-foreground tabular-nums">{s.count}</span>
          </div>
          <div className="mt-1 h-1.5 w-full rounded bg-muted">
            <div
              className="h-1.5 rounded bg-primary"
              style={{ width: `${(s.count / max) * 100}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  )
}
