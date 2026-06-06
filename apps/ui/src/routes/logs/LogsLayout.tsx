import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

const TABS = [
  { path: '/logs/query', label: 'Query' },
  { path: '/logs/signatures', label: 'Signatures' },
  { path: '/logs/models-debug', label: 'Models' },
] as const

export function LogsLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <nav
        aria-label="Logs tabs"
        className="flex gap-1 border-b border-border px-2 pb-2"
        data-testid="logs-tabs"
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
            data-testid={`logs-tab-${tab.path.split('/').pop()}`}
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
