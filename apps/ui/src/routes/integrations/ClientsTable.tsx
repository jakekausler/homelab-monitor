import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import { formatRelative } from '@/lib/relativeTime'

import {
  clientConnection,
  clientDisplayName,
  filterClients,
  sortClients,
  type ClientRow,
  type ClientSortKey,
  type SortDir,
} from './clientsTable'

export function ClientsTable({ rows, query }: { rows: ClientRow[]; query: string }): JSX.Element {
  const [sortKey, setSortKey] = useState<ClientSortKey>('name')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const toggleSort = (key: ClientSortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('asc')
    }
  }

  const sortIndicator = (key: ClientSortKey): string => {
    if (sortKey !== key) return ''
    return sortDir === 'asc' ? ' ▲' : ' ▼'
  }

  // Partition: host row (pinned) + non-host rows (filtered + sorted)
  const hostRow = rows.find((r) => r.is_host) ?? null
  const nonHost = rows.filter((r) => !r.is_host)
  const visible = sortClients(filterClients(nonHost, query), sortKey, sortDir)

  const renderRow = (row: ClientRow, isHost: boolean = false): JSX.Element => (
    <tr key={row.mac} className={isHost ? 'bg-accent/30' : ''}>
      <td className="py-2 pr-3">
        <Link
          to="/integrations/network/clients/$mac"
          params={{ mac: row.mac }}
          className="text-foreground hover:underline"
          data-testid={`client-link-${row.mac}`}
        >
          {clientDisplayName(row)}
        </Link>
        {row.name === null && row.hostname !== null && (
          <span className="ml-2 text-xs text-muted-foreground">{row.hostname}</span>
        )}
        {isHost && (
          <Badge variant="secondary" className="ml-2">
            Host
          </Badge>
        )}
      </td>
      <td className="py-2 pr-3">{row.ip ?? '—'}</td>
      <td className="py-2 pr-3 font-mono text-xs">{row.mac}</td>
      <td className="py-2 pr-3">{row.network ?? '—'}</td>
      <td className="py-2 pr-3">{clientConnection(row)}</td>
      <td className="py-2 pr-3">{formatRelative(row.last_seen)}</td>
      <td className="py-2 pr-3">
        <Badge variant={row.online ? 'ok' : 'muted'}>{row.online ? 'Online' : 'Offline'}</Badge>
      </td>
      <td className="hidden py-2 pr-3 sm:table-cell">{formatRelative(row.lease_expiry)}</td>
      {/* DNS column hook (EPIC-006): when UnifiClientRowModel gains a DNS summary,
          add a <th>/<td> column here. See apps/ui/src/components/clients/README.md. */}
    </tr>
  )

  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No clients found.</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('name')}
                data-testid="clients-sort-name"
              >
                Name{sortIndicator('name')}
              </button>
            </th>
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('ip')}
                data-testid="clients-sort-ip"
              >
                IP{sortIndicator('ip')}
              </button>
            </th>
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('mac')}
                data-testid="clients-sort-mac"
              >
                MAC{sortIndicator('mac')}
              </button>
            </th>
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('network')}
                data-testid="clients-sort-network"
              >
                Network{sortIndicator('network')}
              </button>
            </th>
            <th className="py-2 pr-3 font-medium">Connection</th>
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('last_seen')}
                data-testid="clients-sort-last-seen"
              >
                Last seen{sortIndicator('last_seen')}
              </button>
            </th>
            <th className="py-2 pr-3 font-medium">
              <button
                type="button"
                className="inline-flex items-center hover:text-foreground"
                onClick={() => toggleSort('online')}
                data-testid="clients-sort-online"
              >
                Status{sortIndicator('online')}
              </button>
            </th>
            <th className="hidden py-2 pr-3 font-medium sm:table-cell">Lease expiry</th>
            {/* DNS column hook (EPIC-006): when UnifiClientRowModel gains a DNS summary,
                add a <th>/<td> column here. See apps/ui/src/components/clients/README.md. */}
          </tr>
        </thead>
        <tbody>
          {hostRow && renderRow(hostRow, true)}
          {visible.map((r) => renderRow(r, false))}
        </tbody>
      </table>
      {visible.length === 0 && hostRow === null && (
        <p className="mt-4 text-sm text-muted-foreground">No clients match your search.</p>
      )}
    </div>
  )
}
