import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

const TABS = [
  { path: '/integrations/network/overview', label: 'Overview' },
  { path: '/integrations/network/clients', label: 'Clients' },
] as const

export function NetworkLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-4 pt-4">
        <h1 className="text-lg font-semibold">Network</h1>
        <p className="text-sm text-muted-foreground">
          WAN, DHCP, WiFi experience, and DNS posture.
        </p>
      </div>
      <nav
        aria-label="Network tabs"
        data-testid="network-tabs"
        className="flex gap-1 border-b border-border px-4 pt-2"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            data-testid={`network-tab-${tab.path.split('/').pop()}`}
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
