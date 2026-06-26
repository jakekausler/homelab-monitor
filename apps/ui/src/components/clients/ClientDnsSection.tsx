import type { JSX } from 'react'

import type { Schema } from '@/api/types'
import { formatRelative } from '@/lib/relativeTime'
import { Badge } from '@/components/ui/badge'

import { PanelSection } from '@/routes/integrations/PanelSection'

type DnsEnrichment = Schema<'DnsEnrichment'>

export function ClientDnsSection({ dns }: { dns: DnsEnrichment | null }): JSX.Element {
  if (dns === null) {
    // NULL BRANCH = current reality (EPIC-006 not yet shipped). NEVER fake 0-counts or domains.
    return (
      <PanelSection title="DNS activity">
        <p className="text-sm text-muted-foreground">
          DNS insights provided by Pi-hole — available in a future update.
        </p>
      </PanelSection>
    )
  }

  // POPULATED BRANCH = written NOW against the typed contract for EPIC-006.
  const blockRate = dns.block_rate
  const topBlocked = dns.top_blocked ?? []
  const topPermitted = dns.top_permitted ?? []
  const recentBlocks = dns.recent_blocks ?? []
  const servfail = dns.servfail_count ?? 0
  const dnssecBogus = dns.dnssec_bogus_count ?? 0
  const hasSplit = topBlocked.length > 0 || topPermitted.length > 0
  return (
    <PanelSection title="DNS activity">
      <dl className="grid grid-cols-2 gap-1 text-sm">
        <dt className="text-muted-foreground">Blocked queries</dt>
        <dd>{dns.blocked_count == null ? '—' : dns.blocked_count}</dd>
        <dt className="text-muted-foreground">Query volume</dt>
        <dd>{dns.query_volume ?? '—'}</dd>
        <dt className="text-muted-foreground">Block rate</dt>
        <dd>{blockRate == null ? '—' : `${(blockRate * 100).toFixed(1)}%`}</dd>
        <dt className="text-muted-foreground">Last query</dt>
        <dd>{formatRelative(dns.last_query_at)}</dd>
      </dl>
      {(servfail > 0 || dnssecBogus > 0) && (
        <div className="mt-3 flex gap-2" data-testid="dns-health-badges">
          {servfail > 0 && <Badge variant="warn">{servfail} SERVFAIL</Badge>}
          {dnssecBogus > 0 && <Badge variant="critical">{dnssecBogus} DNSSEC bogus</Badge>}
        </div>
      )}
      {hasSplit ? (
        <div className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          <div data-testid="dns-top-blocked">
            <hr className="mb-2 border-border" />
            <p className="text-sm font-semibold">Top blocked</p>
            {topBlocked.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {topBlocked.map((domain: string) => (
                  <li key={domain} className="break-all text-muted-foreground">
                    {domain}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-muted-foreground">None.</p>
            )}
          </div>
          <div data-testid="dns-top-allowed">
            <hr className="mb-2 border-border" />
            <p className="text-sm font-semibold">Top allowed</p>
            {topPermitted.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {topPermitted.map((domain: string) => (
                  <li key={domain} className="break-all text-muted-foreground">
                    {domain}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-muted-foreground">None.</p>
            )}
          </div>
        </div>
      ) : (
        <p className="mt-3 text-sm text-muted-foreground">No top domains.</p>
      )}
      {recentBlocks.length > 0 && (
        <ul className="mt-3 space-y-1 text-sm" data-testid="dns-recent-blocks">
          {recentBlocks.map((block: { domain: string; at: string }) => (
            <li key={`${block.domain}-${block.at}`} className="break-all text-muted-foreground">
              {block.domain} · {formatRelative(block.at)}
            </li>
          ))}
        </ul>
      )}
    </PanelSection>
  )
}
