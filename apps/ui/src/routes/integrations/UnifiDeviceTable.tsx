import { Link } from '@tanstack/react-router'
import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import { formatUptime } from '@/lib/relativeTime'
import type { Schema } from '@/api/types'

import { formatDeviceKind, formatPct, formatTemp } from './unifiFormat'

type UnifiDeviceRow = Schema<'UnifiDeviceRow'>

export function UnifiDeviceTable({ devices }: { devices: UnifiDeviceRow[] }): JSX.Element {
  if (devices.length === 0) {
    return <p className="text-sm text-muted-foreground">No Unifi devices found.</p>
  }

  const sorted = [...devices].sort((a, b) => a.name.localeCompare(b.name))

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="py-2 pr-3 font-medium">Name</th>
            <th className="py-2 pr-3 font-medium">Model</th>
            <th className="py-2 pr-3 font-medium">Kind</th>
            <th className="py-2 pr-3 font-medium">State</th>
            <th className="py-2 pr-3 font-medium">CPU</th>
            <th className="py-2 pr-3 font-medium">Mem</th>
            <th className="py-2 pr-3 font-medium">Temp</th>
            <th className="py-2 pr-3 font-medium">Uptime</th>
            <th className="py-2 pr-3 font-medium">Update</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((device) => (
            <tr key={device.mac} className="border-b border-border/50 hover:bg-accent/20">
              <td className="py-2 pr-3">
                <Link
                  to="/integrations/unifi/devices/$device"
                  params={{ device: device.mac }}
                  className="text-foreground hover:underline"
                  data-testid={`unifi-device-link-${device.mac}`}
                >
                  {device.name}
                </Link>
              </td>
              <td className="py-2 pr-3 text-muted-foreground">{device.model}</td>
              <td className="py-2 pr-3 text-muted-foreground">{formatDeviceKind(device.kind)}</td>
              <td className="py-2 pr-3">
                <Badge variant={device.up ? 'ok' : 'critical'}>{device.up ? 'Up' : 'Down'}</Badge>
              </td>
              <td className="py-2 pr-3 text-muted-foreground">{formatPct(device.cpu_pct)}</td>
              <td className="py-2 pr-3 text-muted-foreground">{formatPct(device.mem_pct)}</td>
              <td className="py-2 pr-3 text-muted-foreground">{formatTemp(device.temp)}</td>
              <td className="py-2 pr-3 text-muted-foreground">
                {formatUptime(device.uptime_seconds)}
              </td>
              <td className="py-2 pr-3">
                {device.update_available ? <Badge variant="warn">update</Badge> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
