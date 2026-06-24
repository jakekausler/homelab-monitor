import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

import { PiholeStatusStrip } from './PiholeStatusStrip'

const TABS = [
  { path: '/integrations/pihole/overview', label: 'Overview' },
  { path: '/integrations/pihole/logs', label: 'Logs' },
] as const

export function PiholeLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-2 pb-2">
        <h1 className="text-2xl font-semibold tracking-tight">Pi-hole integration</h1>
        <p className="text-sm text-muted-foreground">
          Blocking status, gravity, messages, upstreams, clients, and query logs.
        </p>
      </div>
      <PiholeStatusStrip />
      <nav
        aria-label="Pi-hole tabs"
        className="flex gap-1 border-b border-border px-2 pb-2"
        data-testid="pihole-tabs"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            className="shrink-0 rounded-md border border-transparent px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
            activeProps={{
              className:
                'shrink-0 rounded-md border-border bg-card px-3 py-1.5 text-sm font-medium text-foreground',
            }}
            data-testid={`pihole-tab-${tab.path.split('/').pop()}`}
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
