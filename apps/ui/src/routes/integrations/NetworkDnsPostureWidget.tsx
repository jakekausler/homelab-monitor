import type { JSX } from 'react'

import { CheckCircle2, AlertTriangle } from 'lucide-react'

import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'

type UnifiDnsPostureResponse = Schema<'UnifiDnsPostureResponse'>
type UnifiDnsHandout = UnifiDnsPostureResponse['networks'][number]

function DriftBadge({ handout }: { handout: UnifiDnsHandout }): JSX.Element | null {
  if (handout.drift) {
    return (
      <Badge
        variant="critical"
        aria-label={`DNS drift: ${handout.network} hands out ${handout.dns}, expected ${handout.expected_dns ?? 'unknown'}`}
        className="inline-flex gap-1"
      >
        <AlertTriangle className="size-3.5" />
        Drift
      </Badge>
    )
  }
  if (handout.expected_dns) {
    return (
      <Badge
        variant="ok"
        aria-label={`DNS matches expected steering IP for ${handout.network}`}
        className="inline-flex gap-1"
      >
        <CheckCircle2 className="size-3.5" />
        OK
      </Badge>
    )
  }
  return null
}

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
        <li key={h.network} className="flex items-center justify-between gap-4">
          <span className="text-foreground">{h.network}</span>
          <span className="flex items-center gap-2">
            <span className="text-muted-foreground tabular-nums">{h.dns}</span>
            <DriftBadge handout={h} />
          </span>
        </li>
      ))}
    </ul>
  )
}
