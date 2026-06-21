import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'
import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import { useUnifiClient } from '@/api/unifi'
import { formatRelative, formatAbsolute } from '@/lib/relativeTime'
import { ClientDnsSection } from '@/components/clients/ClientDnsSection'

import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'
import { formatBitrate, formatBytes, formatSignal } from './unifiFormat'

export function NetworkClientPage(): JSX.Element {
  // RAW param — TanStack decodes colons automatically; no decodeURIComponent.
  const { mac } = useParams({ from: '/protected/integrations/network/clients/$mac' })
  const result = useUnifiClient(mac)

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <Link
        to="/integrations/network/clients"
        className="inline-flex items-center text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to clients
      </Link>

      <h2 className="text-base font-semibold">{mac}</h2>

      <QueryState
        result={result}
        unavailableLabel="Client data temporarily unavailable"
        notFoundLabel="Client not found"
        renderData={(data) => (
          <div className="space-y-4">
            {/* Identity */}
            <PanelSection title="Identity">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt className="text-muted-foreground">Name</dt>
                <dd>
                  {data.name ?? data.hostname ?? '—'}
                  {data.is_host && (
                    <Badge variant="secondary" className="ml-2">
                      Host
                    </Badge>
                  )}
                </dd>
                <dt className="text-muted-foreground">Hostname</dt>
                <dd>{data.hostname ?? '—'}</dd>
                <dt className="text-muted-foreground">MAC</dt>
                <dd>{data.mac}</dd>
                <dt className="text-muted-foreground">IP</dt>
                <dd>
                  {data.ip ?? '—'}
                  {data.use_fixedip && data.fixed_ip !== null && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      (fixed: {data.fixed_ip})
                    </span>
                  )}
                </dd>
                <dt className="text-muted-foreground">Device type</dt>
                <dd>{data.oui ?? '—'}</dd>
                <dt className="text-muted-foreground">Network</dt>
                <dd>{data.network ?? '—'}</dd>
                <dt className="text-muted-foreground">Status</dt>
                <dd>
                  <Badge variant={data.online ? 'ok' : 'muted'}>
                    {data.online ? 'Online' : 'Offline'}
                  </Badge>
                </dd>
                <dt className="text-muted-foreground">First seen</dt>
                <dd>{formatRelative(data.first_seen)}</dd>
                <dt className="text-muted-foreground">Last seen</dt>
                <dd>{formatRelative(data.last_seen)}</dd>
              </dl>
            </PanelSection>

            {/* Connection */}
            <PanelSection title="Connection">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                {data.ap_mac !== null ? (
                  <>
                    <dt className="text-muted-foreground">Wi-Fi AP</dt>
                    <dd>Wi-Fi via {data.ap_mac}</dd>
                    <dt className="text-muted-foreground">Signal</dt>
                    <dd>{formatSignal(data.series.signal_dbm)}</dd>
                  </>
                ) : data.sw_mac !== null ? (
                  <>
                    <dt className="text-muted-foreground">Switch</dt>
                    <dd>
                      Switch {data.sw_mac}
                      {data.sw_port !== null ? ` port ${data.sw_port}` : ''}
                    </dd>
                  </>
                ) : (
                  <>
                    <dt className="text-muted-foreground">Link</dt>
                    <dd>—</dd>
                  </>
                )}
              </dl>
            </PanelSection>

            {/* Bandwidth */}
            <PanelSection title="Bandwidth">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt className="text-muted-foreground">TX</dt>
                <dd>{formatBitrate(data.series.tx_rate_bps)}</dd>
                <dt className="text-muted-foreground">RX</dt>
                <dd>{formatBitrate(data.series.rx_rate_bps)}</dd>
              </dl>
            </PanelSection>

            {/* DPI */}
            <PanelSection title="Traffic by application (DPI)">
              {data.dpi.length === 0 ? (
                <p className="text-sm text-muted-foreground">No DPI data for this client.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-xs text-muted-foreground">
                        <th className="py-2 pr-3 font-medium">App</th>
                        <th className="py-2 pr-3 font-medium">Category</th>
                        <th className="py-2 pr-3 font-medium">Bytes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...data.dpi]
                        .sort((a, b) => b.bytes - a.bytes)
                        .map((d) => (
                          <tr key={`${d.app}-${d.cat}`} className="border-b border-border/50">
                            <td className="py-2 pr-3">{d.app}</td>
                            <td className="py-2 pr-3 text-muted-foreground">{d.cat}</td>
                            <td className="py-2 pr-3 text-muted-foreground">
                              {formatBytes(d.bytes)}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}
            </PanelSection>

            {/* Lease */}
            <PanelSection title="DHCP lease">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt className="text-muted-foreground">Lease expiry</dt>
                <dd title={formatAbsolute(data.lease_expiry)}>
                  {formatRelative(data.lease_expiry)}
                </dd>
                <dt className="text-muted-foreground">Fixed IP</dt>
                <dd>{data.use_fixedip ? (data.fixed_ip ?? 'yes') : 'no'}</dd>
              </dl>
            </PanelSection>

            {/* DNS (EPIC-006 slot) */}
            <ClientDnsSection dns={data.dns ?? null} />
          </div>
        )}
      />
    </div>
  )
}
