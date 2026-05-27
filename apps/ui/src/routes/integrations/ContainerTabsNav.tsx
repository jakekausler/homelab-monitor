import { Link } from '@tanstack/react-router'
import type { JSX } from 'react'

interface ContainerTabsNavProps {
  name: string
}

const TABS = [
  { path: 'overview', label: 'Overview' },
  { path: 'probes', label: 'Probes' },
  { path: 'logs', label: 'Logs' },
  { path: 'actions', label: 'Recent Actions' },
] as const

export function ContainerTabsNav({ name }: ContainerTabsNavProps): JSX.Element {
  return (
    <nav
      aria-label="Container tabs"
      className="-mx-2 flex gap-1 overflow-x-auto px-2"
      data-testid="container-tabs"
    >
      {TABS.map((tab) => (
        <Link
          key={tab.path}
          to={`/integrations/docker/containers/$name/${tab.path}`}
          params={{ name }}
          className="shrink-0 rounded-md border border-transparent px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
          activeProps={{
            className:
              'shrink-0 rounded-md border-border bg-card px-3 py-1.5 text-sm font-medium text-foreground',
          }}
          data-testid={`tab-${tab.path}`}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  )
}
