import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'
import { Badge } from '@/components/ui/badge'
import { useUnifiSummary } from '@/api/unifi'

const TABS = [{ path: '/integrations/unifi/overview', label: 'Overview' }] as const

function UnifiStatusStrip(): JSX.Element {
  const result = useUnifiSummary()

  return (
    <div data-testid="unifi-status-strip" className="px-4 pt-2">
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Unifi metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {result.data && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={result.data.controller_up ? 'ok' : 'critical'}>
            Controller {result.data.controller_up ? 'up' : 'down'}
          </Badge>
          <Badge variant={result.data.wan_up ? 'ok' : 'critical'}>
            WAN {result.data.wan_up ? 'up' : 'down'}
          </Badge>
          <Badge variant={result.data.teleport_up ? 'ok' : 'muted'}>
            Teleport {result.data.teleport_up ? 'up' : 'down'}
          </Badge>
          <span className="text-muted-foreground">
            Devices {result.data.devices_up}/{result.data.devices_total}
          </span>
          {result.data.threat_count > 0 ? (
            <Badge variant="warn">{result.data.threat_count} threats</Badge>
          ) : (
            <span className="text-muted-foreground">No threats</span>
          )}
        </div>
      )}
    </div>
  )
}

export function UnifiLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-4 pt-4">
        <h1 className="text-lg font-semibold">Unifi integration</h1>
        <p className="text-sm text-muted-foreground">Unifi gear, network, and clients.</p>
      </div>
      <UnifiStatusStrip />
      <nav
        aria-label="Unifi tabs"
        data-testid="unifi-tabs"
        className="flex gap-1 border-b border-border px-4 pt-2"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            data-testid={`unifi-tab-${tab.path.split('/').pop()}`}
            className="rounded-t-md px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
            activeProps={{
              className: 'rounded-t-md px-3 py-2 text-sm text-foreground border-b-2 border-primary',
            }}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      <div className="min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  )
}
