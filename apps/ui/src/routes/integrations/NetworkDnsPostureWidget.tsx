import type { JSX } from 'react'

import type { Schema } from '@/api/types'

type UnifiDnsPostureResponse = Schema<'UnifiDnsPostureResponse'>

export function NetworkDnsPostureWidget({ data }: { data: UnifiDnsPostureResponse }): JSX.Element {
  if (data.networks.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No per-network DNS overrides configured — clients use the default DHCP DNS steering.
      </p>
    )
  }
  return (
    <ul className="space-y-1 text-sm">
      {data.networks.map((h) => (
        <li key={h.network} className="flex justify-between gap-4">
          <span className="text-foreground">{h.network}</span>
          <span className="text-muted-foreground tabular-nums">{h.dns}</span>
        </li>
      ))}
    </ul>
  )
}
