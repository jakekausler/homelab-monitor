import type { JSX } from 'react'

import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { formatPct } from './unifiFormat'

type UnifiNetworkDhcpResponse = Schema<'UnifiNetworkDhcpResponse'>

export function NetworkDhcpWidget({ data }: { data: UnifiNetworkDhcpResponse }): JSX.Element {
  if (data.networks.length === 0) {
    return <p className="text-sm text-muted-foreground">No DHCP networks reported</p>
  }
  return (
    <ul className="space-y-3 text-sm">
      {data.networks.map((n) => (
        <li key={n.network} className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-foreground">{n.network}</span>
            <Badge variant={n.dhcp_enabled ? 'ok' : 'muted'}>
              DHCP {n.dhcp_enabled ? 'enabled' : 'disabled'}
            </Badge>
          </div>
          {n.dhcp_enabled ? (
            <div className="space-y-0.5 text-muted-foreground">
              <div>
                Pool:{' '}
                {n.pool_start === null && n.pool_end === null ? (
                  '—'
                ) : (
                  <>
                    {n.pool_start ?? '?'}
                    {n.pool_end !== null ? `–${n.pool_end}` : ''}
                  </>
                )}
                {n.pool_size !== null ? ` (${n.pool_size} addresses)` : ''}
              </div>
              <div>
                Occupancy:{' '}
                {n.occupancy === null ? 'occupancy unavailable' : formatPct(n.occupancy * 100)}
              </div>
              <div>Reservations: {n.reservation_count}</div>
            </div>
          ) : (
            <p className="text-muted-foreground">
              DHCP not enabled on this network ({n.reservation_count} reservations)
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}
