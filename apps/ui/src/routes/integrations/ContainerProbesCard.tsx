import { useMemo, useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import {
  useCreateProbeTarget,
  useDeleteProbeTarget,
  useListDockerSuggestions,
  useListProbes,
  useSuggestionDefaultProbes,
} from '@/api/docker'
import type { Schema } from '@/api/types'
import type { ContainerRow } from './types'
import { StatusBadge } from './badges'
import { AddProbeModal } from './AddProbeModal'
import { EditProbeModal } from './EditProbeModal'

type ProbeRow = Schema<'ProbeRow'>

interface ContainerProbesCardProps {
  container: ContainerRow
}

export function ContainerProbesCard({ container }: ContainerProbesCardProps) {
  const probesQuery = useListProbes(container.name)
  const suggestionsQuery = useListDockerSuggestions('pending')

  // Resolve the (optional) suggestion_id for this container by scanning the
  // first page of pending suggestions. STAGE-003-012 Refinement-scope-expansion
  // accepts the current page size of 50 — a future iteration may add
  // a backend lookup. Most homelabs have <50 pending suggestions.
  const suggestionId = useMemo(() => {
    if (!suggestionsQuery.data?.pages) return ''
    const all = suggestionsQuery.data.pages.flatMap((p) => p.suggestions ?? [])
    const match = all.find((s) => s.container_name === container.name)
    return match?.id ?? ''
  }, [suggestionsQuery.data, container.name])

  const defaultsQuery = useSuggestionDefaultProbes(suggestionId)

  const [ignoredKeys, setIgnoredKeys] = useState<Set<string>>(() => new Set())
  const [addOpen, setAddOpen] = useState(false)
  const [editProbe, setEditProbe] = useState<ProbeRow | null>(null)

  const createMutation = useCreateProbeTarget()
  const deleteMutation = useDeleteProbeTarget()

  const activeProbes: ProbeRow[] = probesQuery.data?.probes ?? []
  const activeKeys = new Set(activeProbes.map((p) => `${p.kind}|${p.name}`))

  const suggestedDefaults =
    defaultsQuery.data?.reason === 'available' ? defaultsQuery.data.probes : []

  const filteredSuggested = suggestedDefaults.filter((s) => {
    const key = `${s.kind}|${s.name}`
    return !activeKeys.has(key) && !ignoredKeys.has(key)
  })

  const handleIgnore = (kind: string, name: string) => {
    const key = `${kind}|${name}`
    setIgnoredKeys((prev) => {
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }

  const handleAdd = async (spec: {
    kind: 'http' | 'tcp' | 'exec' | 'metrics'
    name: string
    target_value: string
    interval_seconds: number
    timeout_seconds: number
  }) => {
    try {
      await createMutation.mutateAsync({
        body: {
          container_name: container.name,
          kind: spec.kind,
          name: spec.name,
          target_value: spec.target_value,
          interval_seconds: spec.interval_seconds,
          timeout_seconds: spec.timeout_seconds,
        },
      })
      toast.success(`Added probe ${spec.kind}.${spec.name}`)
    } catch (err) {
      if (err instanceof ApiError) {
        toast.error(err.message || 'Add failed')
      } else {
        toast.error('Add failed')
      }
    }
  }

  const handleDelete = async (probe: ProbeRow) => {
    try {
      await deleteMutation.mutateAsync({
        probeId: probe.id,
        containerName: container.name,
      })
      toast.success(`Deleted ${probe.kind}.${probe.name}`)
    } catch (err) {
      if (err instanceof ApiError) {
        toast.error(err.message || 'Delete failed')
      } else {
        toast.error('Delete failed')
      }
    }
  }

  return (
    <article
      className="space-y-3 rounded-md border border-border bg-card p-4"
      data-testid="container-probes-card"
      data-container-name={container.name}
    >
      <header className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold tracking-tight">{container.name}</h3>
          <p className="truncate text-xs text-muted-foreground" title={container.image ?? ''}>
            {container.image ?? '—'}
          </p>
        </div>
        {container.status && <StatusBadge status={container.status} />}
      </header>

      {/* Active probes */}
      <section aria-label="Active probes" className="space-y-1">
        <h4 className="text-xs font-semibold text-muted-foreground">Active probes</h4>
        {probesQuery.isLoading && (
          <p className="text-xs text-muted-foreground">Loading active probes…</p>
        )}
        {!probesQuery.isLoading && activeProbes.length === 0 && (
          <p className="text-xs text-muted-foreground" data-testid="no-active-probes">
            No active probes
          </p>
        )}
        {!probesQuery.isLoading && activeProbes.length > 0 && (
          <ul className="space-y-1" data-testid="active-probes-list">
            {activeProbes.map((p) => (
              <li
                key={p.id}
                className="flex items-center justify-between gap-2 rounded border border-border bg-background px-2 py-1 text-xs"
                data-testid={`active-probe-${p.id}`}
              >
                <div className="min-w-0 flex items-center gap-2">
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
                    {p.kind}
                  </span>
                  <span className="font-medium">{p.name}</span>
                  <span className="truncate text-muted-foreground" title={p.target_value}>
                    {p.target_value}
                  </span>
                  <span className="text-muted-foreground">
                    {p.interval_seconds}s / {p.timeout_seconds}s
                  </span>
                </div>
                <div className="flex gap-1">
                  <button
                    type="button"
                    className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-accent"
                    onClick={() => setEditProbe(p)}
                    disabled={p.id.startsWith('optimistic-')}
                    data-testid={`active-probe-edit-${p.id}`}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-destructive/10"
                    onClick={() => void handleDelete(p)}
                    disabled={deleteMutation.isPending || p.id.startsWith('optimistic-')}
                    data-testid={`active-probe-delete-${p.id}`}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Suggested probes — only show if there's a pending suggestion. */}
      {suggestionId !== '' && (
        <section aria-label="Suggested probes" className="space-y-1">
          <h4 className="text-xs font-semibold text-muted-foreground">Suggested probes</h4>
          {defaultsQuery.isLoading && (
            <p className="text-xs text-muted-foreground">Loading suggested defaults…</p>
          )}
          {!defaultsQuery.isLoading && filteredSuggested.length === 0 && (
            <p className="text-xs text-muted-foreground" data-testid="no-suggested-probes">
              No suggested probes
            </p>
          )}
          {!defaultsQuery.isLoading && filteredSuggested.length > 0 && (
            <ul className="space-y-1" data-testid="suggested-probes-list">
              {filteredSuggested.map((s) => {
                const rowKey = `${s.kind}|${s.name}`
                return (
                  <li
                    key={rowKey}
                    className="flex items-center justify-between gap-2 rounded border border-dashed border-border bg-background px-2 py-1 text-xs"
                    data-testid={`suggested-probe-${rowKey}`}
                  >
                    <div className="min-w-0 flex items-center gap-2">
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
                        {s.kind}
                      </span>
                      <span className="font-medium">{s.name}</span>
                      <span className="truncate text-muted-foreground" title={s.target_value}>
                        {s.target_value}
                      </span>
                    </div>
                    <div className="flex gap-1">
                      <button
                        type="button"
                        className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-accent"
                        onClick={() =>
                          void handleAdd({
                            kind: s.kind,
                            name: s.name,
                            target_value: s.target_value,
                            interval_seconds: s.interval_seconds,
                            timeout_seconds: s.timeout_seconds,
                          })
                        }
                        disabled={createMutation.isPending}
                        data-testid={`suggested-probe-add-${rowKey}`}
                      >
                        Add
                      </button>
                      <button
                        type="button"
                        className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-accent"
                        onClick={() => handleIgnore(s.kind, s.name)}
                        data-testid={`suggested-probe-ignore-${rowKey}`}
                      >
                        Ignore
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      )}

      <div>
        <button
          type="button"
          className="rounded border border-border bg-card px-3 py-1 text-xs hover:bg-accent"
          onClick={() => setAddOpen(true)}
          data-testid="add-new-probe-button"
        >
          Add new probe
        </button>
      </div>

      <AddProbeModal containerName={container.name} open={addOpen} onOpenChange={setAddOpen} />
      {editProbe && (
        <EditProbeModal
          probe={editProbe}
          open={editProbe !== null}
          onOpenChange={(open) => {
            if (!open) setEditProbe(null)
          }}
        />
      )}
    </article>
  )
}
