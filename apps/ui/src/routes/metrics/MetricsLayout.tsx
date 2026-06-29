import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

const TABS = [
  { path: '/metrics/system', label: 'System' },
  { path: '/metrics/containers', label: 'Containers' },
  { path: '/metrics/collectors', label: 'Collectors' },
  { path: '/metrics/heartbeats', label: 'Heartbeats' },
  { path: '/metrics/storage-logs', label: 'Storage & Logs' },
  { path: '/metrics/home-assistant', label: 'Home Assistant' },
  { path: '/metrics/unifi', label: 'Unifi' },
  { path: '/metrics/network', label: 'Network' },
  { path: '/metrics/pihole', label: 'Pi-hole' },
  { path: '/metrics/synology', label: 'Synology' },
  { path: '/metrics/surveillance', label: 'Surveillance' },
] as const

export function MetricsLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-2 pb-2">
        <h1 className="text-2xl font-semibold tracking-tight">Metrics</h1>
        <p className="text-sm text-muted-foreground">
          Grafana dashboards for system host overview and Home Assistant.
        </p>
      </div>
      <nav
        aria-label="Metrics tabs"
        className="-mx-2 flex gap-1 overflow-x-auto border-b border-border px-2 pb-2"
        data-testid="metrics-tabs"
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
            data-testid={`metrics-tab-${tab.path.split('/').pop()}`}
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
