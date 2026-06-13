import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

const TABS = [
  { path: '/integrations/home-assistant/health', label: 'Health' },
  { path: '/integrations/home-assistant/status', label: 'Status' },
  { path: '/integrations/home-assistant/logs', label: 'Logs' },
] as const

export function HomeAssistantLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-2 pb-2">
        <h1 className="text-2xl font-semibold tracking-tight">Home Assistant integration</h1>
        <p className="text-sm text-muted-foreground">
          Entity health, battery, updates, integration status, and logs.
        </p>
      </div>
      <nav
        aria-label="Home Assistant tabs"
        className="flex gap-1 border-b border-border px-2 pb-2"
        data-testid="home-assistant-tabs"
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
            data-testid={`home-assistant-tab-${tab.path.split('/').pop()}`}
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
