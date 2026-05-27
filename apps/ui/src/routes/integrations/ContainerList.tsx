import { useMemo } from 'react'
import type { JSX } from 'react'
import { Boxes } from 'lucide-react'

import { useListContainers } from '@/api/docker'
import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { extractComposeBasename } from './composeBasename'
import { ContainerListRow } from './ContainerListRow'
import type { ContainerRow } from './types'

export function ContainerList(): JSX.Element {
  const result = useListContainers()

  const groups = useMemo(() => {
    const containers = result.data?.containers ?? []
    const buckets = new Map<string, ContainerRow[]>()
    for (const c of containers) {
      const key = extractComposeBasename(c.compose_file_path) ?? 'Ungrouped'
      const arr = buckets.get(key) ?? []
      arr.push(c)
      buckets.set(key, arr)
    }
    for (const arr of buckets.values()) {
      arr.sort((a, b) => a.name.localeCompare(b.name))
    }
    // Ungrouped goes last; everything else alphabetical.
    const labels = [...buckets.keys()].sort((a, b) => {
      if (a === 'Ungrouped') return 1
      if (b === 'Ungrouped') return -1
      return a.localeCompare(b)
    })
    return labels.map((label) => ({ label, items: buckets.get(label) ?? [] }))
  }, [result.data])

  return (
    <div className="space-y-4" data-testid="container-list">
      {result.error?.status === 503 && (
        <div className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800">
          Container data temporarily unavailable
        </div>
      )}
      {result.isPending && <p className="text-sm text-muted-foreground">Loading containers…</p>}
      {result.isError && result.error.status !== 503 && <ErrorDisplay error={result.error} />}
      {result.data && (result.data.containers ?? []).length === 0 && (
        <EmptyState>No containers found.</EmptyState>
      )}
      {groups.map(({ label, items }) => (
        <section key={label} aria-labelledby={`group-${label}`} className="space-y-1">
          <h2
            id={`group-${label}`}
            className="flex items-center gap-2 px-2 py-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            <Boxes className="size-4" />
            {label}
            <span className="text-xs text-muted-foreground">({items.length})</span>
          </h2>
          <ul className="space-y-1">
            {items.map((c) => (
              <li key={c.name}>
                <ContainerListRow container={c} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
