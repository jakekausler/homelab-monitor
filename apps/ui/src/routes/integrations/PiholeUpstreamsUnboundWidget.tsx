import type { JSX } from 'react'

import { useUnbound, useUpstreams } from '@/api/pihole'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'

function pct(ratio: number | null): string {
  return ratio === null ? '—' : `${(ratio * 100).toFixed(1)}%`
}

function ms(seconds: number | null): string {
  return seconds === null ? '—' : `${(seconds * 1000).toFixed(1)} ms`
}

function intish(value: number | null): string {
  return value === null ? '—' : value.toLocaleString()
}

export function PiholeUpstreamsUnboundWidget(): JSX.Element {
  const upstreams = useUpstreams()
  const unbound = useUnbound()

  return (
    <div data-testid="pihole-upstreams-unbound-widget" className="space-y-4 text-sm">
      <div>
        <h3 className="mb-2 font-semibold">Upstreams</h3>
        <QueryState
          result={upstreams}
          unavailableLabel="Pi-hole upstreams temporarily unavailable"
          renderData={(data) => {
            const sorted = [...data.rows].sort((a, b) => b.queries - a.queries)
            if (sorted.length === 0) {
              return <EmptyState testId="pihole-upstreams-empty">No upstream data</EmptyState>
            }
            return (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-border">
                    <tr>
                      <th className="px-2 py-1">Upstream</th>
                      <th className="hidden px-2 py-1 text-right font-semibold sm:table-cell">
                        Queries
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {sorted.map((row) => (
                      <tr key={row.upstream}>
                        <td className="px-2 py-1">{row.upstream}</td>
                        <td className="hidden px-2 py-1 text-right font-mono sm:table-cell">
                          {row.queries.toLocaleString()}
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

      <div>
        <h3 className="mb-2 font-semibold">Unbound</h3>
        <QueryState
          result={unbound}
          unavailableLabel="Pi-hole unbound temporarily unavailable"
          renderData={(data) => (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span>Cache-hit ratio</span>
                <span className="font-mono">{pct(data.cache_hit_ratio)}</span>
              </div>

              <div className="flex items-center justify-between">
                <span>Extended stats</span>
                <Badge
                  variant={data.extended_stats_enabled === true ? 'ok' : 'muted'}
                  className="text-xs"
                >
                  {data.extended_stats_enabled === true
                    ? 'Extended stats on'
                    : 'Extended stats off'}
                </Badge>
              </div>

              {data.extended_stats_enabled === true ? (
                <>
                  <div className="flex items-center justify-between">
                    <span>Recursion p50</span>
                    <span className="font-mono">{ms(data.recursion_p50_seconds)}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Recursion p95</span>
                    <span className="font-mono">{ms(data.recursion_p95_seconds)}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>DNSSEC secure</span>
                    <span className="font-mono">{intish(data.dnssec_secure_total)}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>DNSSEC bogus</span>
                    {data.dnssec_bogus_total != null && data.dnssec_bogus_total > 0 ? (
                      <Badge variant="warn" className="text-xs">
                        {data.dnssec_bogus_total.toLocaleString()}
                      </Badge>
                    ) : (
                      <span className="font-mono">{intish(data.dnssec_bogus_total)}</span>
                    )}
                  </div>
                  <div className="flex items-center justify-between">
                    <span>SERVFAIL</span>
                    <span className="font-mono">{intish(data.servfail_total)}</span>
                  </div>
                </>
              ) : (
                <p className="text-xs text-muted-foreground">Unbound extended stats disabled</p>
              )}
            </div>
          )}
        />
      </div>
    </div>
  )
}
