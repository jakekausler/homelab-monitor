import type { JSX } from 'react'

import { useClients } from '@/api/pihole'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'

export interface MergedClientRow {
  client: string
  name: string | null
  total: number
  blocked: number
  blockPct: number
}

export function mergeClients(
  totalRows: readonly { client: string; name: string | null; count: number }[],
  blockedRows: readonly { client: string; name: string | null; count: number }[],
): MergedClientRow[] {
  const blockedByClient = new Map<string, number>()
  for (const r of blockedRows) blockedByClient.set(r.client, r.count)
  const merged: MergedClientRow[] = totalRows.map((r) => {
    const blocked = blockedByClient.get(r.client) ?? 0
    const blockPct = r.count > 0 ? (blocked / r.count) * 100 : 0
    return { client: r.client, name: r.name, total: r.count, blocked, blockPct }
  })
  merged.sort((a, b) => b.total - a.total)
  return merged
}

export function PiholeClientsWidget(): JSX.Element {
  const total = useClients(false)
  const blocked = useClients(true)

  return (
    <div data-testid="pihole-clients-widget" className="text-sm">
      <QueryState
        result={total}
        unavailableLabel="Pi-hole clients temporarily unavailable"
        renderData={(totalData) => {
          const blockedData = blocked.data
          const merged = mergeClients(totalData.rows, blockedData?.rows ?? [])

          if (merged.length === 0) {
            return <EmptyState testId="pihole-clients-empty">No client data</EmptyState>
          }

          return (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-border">
                  <tr>
                    <th className="px-2 py-1">Client</th>
                    <th className="px-2 py-1 text-right font-semibold tabular-nums">Total</th>
                    <th className="hidden px-2 py-1 text-right font-semibold tabular-nums sm:table-cell">
                      Blocked
                    </th>
                    <th className="px-2 py-1 text-right font-semibold tabular-nums">Block %</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {merged.map((row) => (
                    <tr key={row.client}>
                      <td className="px-2 py-1">
                        {row.name ? (
                          <div>
                            <div className="font-semibold">{row.name}</div>
                            <div className="text-xs text-muted-foreground font-mono">
                              {row.client}
                            </div>
                          </div>
                        ) : (
                          <span className="font-mono">{row.client}</span>
                        )}
                      </td>
                      <td className="px-2 py-1 text-right tabular-nums">
                        {row.total.toLocaleString()}
                      </td>
                      <td className="hidden px-2 py-1 text-right tabular-nums sm:table-cell">
                        {row.blocked.toLocaleString()}
                      </td>
                      <td className="px-2 py-1 text-right tabular-nums">
                        {row.blockPct.toFixed(1)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }}
      />
    </div>
  )
}
