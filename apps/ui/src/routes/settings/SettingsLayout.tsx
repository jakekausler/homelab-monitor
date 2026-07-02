import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

const TABS = [
  { path: '/settings/logs', label: 'Logs' },
  { path: '/settings/autofix', label: 'Auto-fix' },
] as const

export function SettingsLayout(): JSX.Element {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Logs retention and disk budget today; more settings land in upcoming epics.
        </p>
      </div>
      <nav
        aria-label="Settings tabs"
        className="flex gap-1 border-b border-border pb-2"
        data-testid="settings-tabs"
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
            data-testid={`settings-tab-${tab.path.split('/').pop() ?? ''}`}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      <Outlet />
    </div>
  )
}
