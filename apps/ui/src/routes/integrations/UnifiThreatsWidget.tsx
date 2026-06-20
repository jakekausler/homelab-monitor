import type { JSX } from 'react'

import type { Schema } from '@/api/types'

type UnifiThreatRow = Schema<'UnifiThreatRow'>

export function UnifiThreatsWidget({ threats }: { threats: UnifiThreatRow[] }): JSX.Element {
  if (threats.length === 0) {
    return <p className="text-sm text-muted-foreground">No active threats</p>
  }
  return (
    <ul className="space-y-1 text-sm">
      {threats.map((t) => (
        <li key={t.threat_type} className="flex justify-between">
          <span className="text-foreground">{t.threat_type}</span>
          <span className="text-muted-foreground">{t.count}</span>
        </li>
      ))}
    </ul>
  )
}
