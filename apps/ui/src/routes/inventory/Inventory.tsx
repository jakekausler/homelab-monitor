import { Link, Outlet } from '@tanstack/react-router'

import { cn } from '@/lib/utils'

const TABS = [
  {
    label: 'Crons',
    to: '/inventory/crons' as const,
    search: {
      page: 1,
      page_size: 100,
      host: undefined,
      enabled: undefined,
      state: undefined,
      q: undefined,
      include_hidden: false,
    },
  },
] as const

export function InventoryLayout() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Inventory</h1>
        <p className="text-sm text-muted-foreground">
          Crons today; containers, hosts, and integrations land in upcoming epics.
        </p>
      </div>
      <nav aria-label="Inventory sections" className="flex gap-2 border-b border-border">
        {TABS.map((tab) => (
          <Link
            key={tab.to}
            to={tab.to}
            search={tab.search}
            className={cn(
              'px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground',
            )}
            activeProps={{
              className: 'border-b-2 border-primary text-foreground',
            }}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      <Outlet />
    </div>
  )
}
