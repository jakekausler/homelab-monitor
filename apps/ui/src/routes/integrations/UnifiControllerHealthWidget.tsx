import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import { formatDuration } from '@/lib/relativeTime'
import type { Schema } from '@/api/types'

type UnifiControllerHealth = Schema<'UnifiControllerHealth'>

export function UnifiControllerHealthWidget({
  health,
}: {
  health: UnifiControllerHealth
}): JSX.Element {
  return (
    <div className="space-y-2 text-sm">
      <div className="flex justify-between">
        <span className="text-muted-foreground">Controller</span>
        <Badge variant={health.controller_up ? 'ok' : 'critical'}>
          {health.controller_up ? 'up' : 'down'}
        </Badge>
      </div>
      {health.up_reasons.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-muted-foreground">
          {health.up_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      {health.api_took_seconds.length === 0 ? (
        <p className="text-xs text-muted-foreground">No API latency data</p>
      ) : (
        <>
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>Endpoint</span>
            <span>Latency</span>
          </div>
          <ul className="space-y-1">
            {health.api_took_seconds.map((row) => (
              <li key={row.endpoint} className="flex justify-between">
                <span className="truncate text-foreground">{row.endpoint}</span>
                <span className="text-muted-foreground">{formatDuration(row.seconds)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
