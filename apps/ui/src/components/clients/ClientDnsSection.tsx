import type { JSX } from 'react'

import type { Schema } from '@/api/types'
import { formatRelative } from '@/lib/relativeTime'

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
  return (
    <PanelSection title="DNS activity">
      <dl className="grid grid-cols-2 gap-1 text-sm">
        <dt className="text-muted-foreground">Blocked queries</dt>
        <dd>{dns.blocked_count == null ? '—' : dns.blocked_count}</dd>
        <dt className="text-muted-foreground">Last query</dt>
        <dd>{formatRelative(dns.last_query_at)}</dd>
      </dl>
      {dns.top_domains.length > 0 ? (
        <ul className="mt-3 space-y-1 text-sm" data-testid="dns-top-domains">
          {dns.top_domains.map((domain) => (
            <li key={domain} className="text-muted-foreground">
              {domain}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-muted-foreground">No top domains.</p>
      )}
    </PanelSection>
  )
}
