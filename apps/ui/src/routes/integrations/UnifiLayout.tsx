import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

// SCAFFOLDING: STAGE-020/022/023 add child createRoute tabs here (devices, network, clients).
const TABS = [{ path: '/integrations/unifi/overview', label: 'Overview' }] as const

export function UnifiLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-2 pb-2">
        <h1 className="text-2xl font-semibold tracking-tight">Unifi integration</h1>
        <p className="text-sm text-muted-foreground">
          Unifi gear, network, and clients land in upcoming stages.
        </p>
      </div>
      <nav
        aria-label="Unifi tabs"
        className="flex gap-1 border-b border-border px-2 pb-2"
        data-testid="unifi-tabs"
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
            data-testid={`unifi-tab-${tab.path.split('/').pop()}`}
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
