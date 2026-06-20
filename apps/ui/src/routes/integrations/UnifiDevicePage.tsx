import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'
import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'
import { useUnifiDevice } from '@/api/unifi'

import { PanelSection } from './PanelSection'
import { formatBitrate, formatPct, formatSatisfaction } from './unifiFormat'

export function UnifiDevicePage(): JSX.Element {
  // RAW param — TanStack decodes colons automatically; no decodeURIComponent.
  const { device } = useParams({ from: '/protected/integrations/unifi/devices/$device' })
  const result = useUnifiDevice(device)

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <Link
        to="/integrations/unifi/overview"
        className="inline-flex items-center text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to Unifi overview
      </Link>

      <h2 className="text-base font-semibold">{device}</h2>

      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Unifi device data temporarily unavailable
        </div>
      )}
      {result.error?.status === 404 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Device not found
        </div>
      )}
      {result.isError && result.error.status !== 502 && result.error.status !== 404 && (
        <ErrorDisplay error={result.error} />
      )}

      {result.data && (
        <div className="space-y-4">
          <PanelSection title="System">
            <dl className="grid grid-cols-2 gap-1 text-sm">
              <dt className="text-muted-foreground">CPU</dt>
              <dd>{formatPct(result.data.cpu_pct)}</dd>
              <dt className="text-muted-foreground">Memory</dt>
              <dd>{formatPct(result.data.mem_pct)}</dd>
              <dt className="text-muted-foreground">Load avg</dt>
              <dd>{result.data.load == null ? '—' : result.data.load.toFixed(2)}</dd>
            </dl>
            {result.data.temps.length > 0 && (
              <ul className="mt-2 space-y-1 text-sm">
                {result.data.temps.map((temp, i) => (
                  <li key={`temp-${i}`} className="flex flex-wrap gap-2 text-muted-foreground">
                    {Object.entries(temp).map(([k, v]) => (
                      <span key={k}>
                        {k}: {String(v)}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            )}
          </PanelSection>

          {result.data.ports.length > 0 && (
            <PanelSection title="Ports">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">Port</th>
                      <th className="py-2 pr-3 font-medium">State</th>
                      <th className="py-2 pr-3 font-medium">Speed</th>
                      <th className="py-2 pr-3 font-medium">Satisfaction</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.data.ports.map((port) => (
                      <tr key={port.port_idx} className="border-b border-border/50">
                        <td className="py-2 pr-3">{port.port_idx}</td>
                        <td className="py-2 pr-3 text-muted-foreground">
                          {port.up ? 'Up' : 'Down'}
                        </td>
                        <td className="py-2 pr-3 text-muted-foreground">
                          {formatBitrate(port.speed_bps)}
                        </td>
                        <td className="py-2 pr-3 text-muted-foreground">
                          {formatSatisfaction(port.satisfaction)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </PanelSection>
          )}

          {result.data.radios.length > 0 && (
            <PanelSection title="Radios">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">Radio</th>
                      <th className="py-2 pr-3 font-medium">Channel</th>
                      <th className="py-2 pr-3 font-medium">Clients</th>
                      <th className="py-2 pr-3 font-medium">Satisfaction</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.data.radios.map((radio) => (
                      <tr key={radio.radio} className="border-b border-border/50">
                        <td className="py-2 pr-3">{radio.radio}</td>
                        <td className="py-2 pr-3 text-muted-foreground">{radio.channel ?? '—'}</td>
                        <td className="py-2 pr-3 text-muted-foreground">{radio.num_sta ?? '—'}</td>
                        <td className="py-2 pr-3 text-muted-foreground">
                          {formatSatisfaction(radio.satisfaction)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </PanelSection>
          )}

          {result.data.outlets.length > 0 && (
            <PanelSection title="Outlets">
              <ul className="space-y-1 text-sm">
                {result.data.outlets.map((outlet) => (
                  <li key={outlet.outlet} className="flex justify-between">
                    <span className="text-foreground">{outlet.name}</span>
                    <span className="text-muted-foreground">
                      {outlet.relay_state ? 'On' : 'Off'}
                    </span>
                  </li>
                ))}
              </ul>
            </PanelSection>
          )}

          {result.data.ports.length === 0 &&
            result.data.radios.length === 0 &&
            result.data.outlets.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No detail series available for this device.
              </p>
            )}
        </div>
      )}
    </div>
  )
}
