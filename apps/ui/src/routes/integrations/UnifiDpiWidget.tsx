import type { JSX } from 'react'

import type { Schema } from '@/api/types'

import { formatBytes } from './unifiFormat'

type UnifiDpiRow = Schema<'UnifiDpiRow'>

export function UnifiDpiWidget({ apps }: { apps: UnifiDpiRow[] }): JSX.Element {
  if (apps.length === 0) {
    return <p className="text-sm text-muted-foreground">No DPI data</p>
  }
  // Top apps by bytes (descending). Keys: app, bytes, cat, client.
  const sorted = [...apps].sort((a, b) => b.bytes - a.bytes)
  return (
    <ul className="space-y-1 text-sm">
      {sorted.map((row, i) => (
        <li key={`${row.app}-${row.client}-${i}`} className="flex justify-between gap-2">
          <span className="truncate text-foreground">
            {row.app}
            <span className="ml-1 text-xs text-muted-foreground">({row.cat})</span>
          </span>
          <span className="shrink-0 text-muted-foreground">{formatBytes(row.bytes)}</span>
        </li>
      ))}
    </ul>
  )
}
