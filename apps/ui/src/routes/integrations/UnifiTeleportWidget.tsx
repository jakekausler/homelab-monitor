import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import type { Schema } from '@/api/types'

type UnifiTeleport = Schema<'UnifiTeleport'>

export function UnifiTeleportWidget({ teleport }: { teleport: UnifiTeleport }): JSX.Element {
  return (
    <dl className="space-y-1 text-sm">
      <div className="flex justify-between">
        <dt className="text-muted-foreground">Status</dt>
        <dd>
          <Badge variant={teleport.teleport_up ? 'ok' : 'muted'}>
            {teleport.teleport_up ? 'up' : 'down'}
          </Badge>
        </dd>
      </div>
      <div className="flex justify-between">
        <dt className="text-muted-foreground">Version</dt>
        <dd className="text-foreground">{teleport.version ?? '—'}</dd>
      </div>
      {teleport.reason ? (
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Reason</dt>
          <dd className="text-foreground">{teleport.reason}</dd>
        </div>
      ) : null}
    </dl>
  )
}
